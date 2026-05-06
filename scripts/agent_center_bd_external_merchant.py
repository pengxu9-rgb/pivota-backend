#!/usr/bin/env python3
"""
BD tool: AI visibility report for an external (not-yet-onboarded) merchant.

Use this to generate sales-ready evidence for a BD pitch. Pick a brand
that's NOT on Pivota yet, point at one of their flagship products, and
this script will run Demand Test probes against Gemini's grounded
search and produce a markdown report showing:

  - Whether Gemini surfaces the product at all when consumers ask
    "where to buy X" (open visibility)
  - Whether Gemini cites the merchant's own canonical URL when it
    does surface the product (direct attribution) — vs. directing
    consumers to competitor stores
  - The actual competitor URLs Gemini cited instead, ranked by host
    frequency

The output is a markdown report you can hand to the BD team or share
directly with the merchant as evidence: "Here's what AI shopping
agents see when consumers search for products like yours. Here's why
you're losing this channel today. Here's what changes when you
onboard Pivota."

Distinct from `agent_center_baseline.py`:
  - That script validates the demand-test pipeline against KNOWN-good
    inputs (a verified Pivota PDP). PASS/FAIL output. Internal use.
  - THIS script runs against an EXTERNAL merchant who has no Pivota
    presence yet. Markdown report. BD use.

Usage:

  PIVOTA_AGENT_INTERNAL_API_KEY=...    \\
  PIVOTA_AGENT_INTERNAL_BASE_URL=...   \\
  python scripts/agent_center_bd_external_merchant.py \\
    --merchant-name "Glossier" \\
    --merchant-pdp-url https://www.glossier.com/products/cloud-paint \\
    --product-title "Cloud Paint" \\
    --product-vendor "Glossier" \\
    --product-type "blush" \\
    --output reports/glossier-cloud-paint.md

Defaults to provider=gemini, max_runs=3 (post-#280 conservative
default; bump in code if you need tighter signal AND have validated
your Gemini quota can absorb it). Each run = ~3 grounded Gemini calls
× ~25k tokens. Single product test = ~75k tokens. Budget accordingly.

Exit codes:
  0  — report written successfully (or printed)
  1  — probe failed (network / upstream error)
  2  — invalid arguments
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse


# ---------------------------------------------------------------------------
# Probe orchestration
# ---------------------------------------------------------------------------


async def _run_probe(
    *,
    scan_mode: str,
    merchant_name: str,
    context: Dict[str, Any],
    provider: str,
    max_runs: int,
) -> Dict[str, Any]:
    from services import agent_center_llm_client as llm_client

    # Synthetic IDs — the upstream probe only uses these as metadata
    # bookkeeping, never resolves them against any DB. Stable hash from
    # merchant_name keeps the same lead's runs grouped if you re-test.
    safe = "".join(c if c.isalnum() else "_" for c in merchant_name.lower())[:32] or "unknown"
    merchant_id = f"external_bd_{safe}"
    store_id = f"{merchant_id}_lead"
    scan_target_id = f"bd-{scan_mode}-{merchant_id}-{os.urandom(3).hex()}"

    return await llm_client.probe(
        scan_mode=scan_mode,
        scan_target_id=scan_target_id,
        merchant_id=merchant_id,
        store_id=store_id,
        context=context,
        provider=provider,
        max_runs=max_runs,
    )


# ---------------------------------------------------------------------------
# Pure analysis: extract cited URLs + group by host, rank by frequency.
# ---------------------------------------------------------------------------


def _normalize_host(url: str) -> Optional[str]:
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


def _extract_cited_hosts(
    raw_runs: List[Dict[str, Any]],
    *,
    merchant_host: Optional[str],
) -> Tuple[Counter, int, int]:
    """Walk every run's grounding_chunks (the URLs Gemini ACTUALLY cited)
    and return:
      - Counter of {competitor_host: occurrences}
      - count of runs that cited the merchant_host
      - count of runs that cited at least one URL
    Merchant_host is excluded from the competitor counter."""
    competitors: Counter = Counter()
    merchant_cited_runs = 0
    runs_with_any_citation = 0
    for run in raw_runs or []:
        chunks = run.get("grounding_chunks") or []
        if not chunks:
            continue
        runs_with_any_citation += 1
        run_hosts = set()
        merchant_in_run = False
        for url in chunks:
            host = _normalize_host(url) if isinstance(url, str) else None
            if host:
                run_hosts.add(host)
        if merchant_host and merchant_host in run_hosts:
            merchant_in_run = True
            run_hosts.discard(merchant_host)
        if merchant_in_run:
            merchant_cited_runs += 1
        for host in run_hosts:
            competitors[host] += 1
    return competitors, merchant_cited_runs, runs_with_any_citation


def _verdict_for(visibility_score: int, attribution_score: int) -> Tuple[str, str]:
    """Return (verdict_label, BD-friendly explanation paragraph)."""
    if visibility_score < 30 and attribution_score < 30:
        return (
            "INVISIBLE",
            "AI shopping agents don't surface this product at all when consumers ask "
            "natural buyer queries. The merchant has effectively zero presence in this "
            "channel today. As consumer search continues to migrate from Google to "
            "ChatGPT / Gemini / Perplexity, the merchant is losing access to a fast-"
            "growing acquisition surface they have no way to influence directly.",
        )
    if attribution_score < 30 and visibility_score >= 30:
        return (
            "VISIBLE BUT MISATTRIBUTED",
            "AI agents recognize this product but consistently direct consumers to "
            "third-party retailers (marketplaces, beauty blogs, competitor stores) "
            "instead of the merchant's own site. Every cited URL that's not the "
            "merchant's is lost organic traffic — and a margin hit if the cited path "
            "is a third-party reseller. This is the highest-impact failure mode: the "
            "demand exists, it's just being captured by competitors.",
        )
    if visibility_score >= 60 and attribution_score >= 60:
        return (
            "STRONG",
            "AI agents reliably surface this product AND cite the merchant's own "
            "canonical URL as the buying path. This is the goal state — the merchant "
            "owns their AI-channel attribution. Pivota's role here is monitoring + "
            "drift detection, not foundational repair.",
        )
    return (
        "PARTIAL",
        "Mixed result — the product gets surfaced sometimes, and gets attributed "
        "to the merchant's own URL sometimes, but neither is consistent. Worth "
        "investigating which queries fail (see the table below) to identify the "
        "specific gaps before pitching a full Pivota onboarding.",
    )


# ---------------------------------------------------------------------------
# Markdown report renderer
# ---------------------------------------------------------------------------


def _format_query_table(raw_runs: List[Dict[str, Any]], judge_key: str) -> str:
    """Per-query table: did Gemini's answer indicate a positive result?
    `judge_key` is the parsed self-report field for the scan_mode
    (e.g. 'product_visible' or 'merchant_url_found'). We READ the
    self-report here for the per-query column, but the AGGREGATE
    score uses post-hoc URL match (see PR #1296). Both are useful:
    self-report shows the LLM's confidence, post-hoc shows ground truth.
    """
    if not raw_runs:
        return "_(no queries ran)_"
    lines = ["| Query | Gemini said yes? | URL cited (top 1) |", "|---|---|---|"]
    for run in raw_runs:
        q = (run.get("query") or "").strip()
        parsed = run.get("parsed") or {}
        self_report = bool(parsed.get(judge_key))
        symbol = "✅" if self_report else "❌"
        chunks = run.get("grounding_chunks") or []
        top_chunk = chunks[0] if chunks else ""
        # Truncate long URLs for readability.
        if len(top_chunk) > 70:
            top_chunk = top_chunk[:67] + "…"
        lines.append(f"| {q} | {symbol} | {top_chunk or '_(no grounded source)_'} |")
    return "\n".join(lines)


def _format_competitor_table(competitors: Counter, top_n: int = 8) -> str:
    if not competitors:
        return "_(none — Gemini didn't cite any URLs in its answers)_"
    lines = ["| Competitor host | Times cited |", "|---|---|"]
    for host, count in competitors.most_common(top_n):
        lines.append(f"| `{host}` | {count} |")
    return "\n".join(lines)


def render_markdown_report(args: Dict[str, Any]) -> str:
    """Build the full markdown report. Pure function — easy to test."""
    merchant_name = args["merchant_name"]
    merchant_pdp_url = args["merchant_pdp_url"]
    product_title = args["product_title"]
    product_vendor = args.get("product_vendor") or ""
    product_type = args.get("product_type") or ""
    visibility_result = args["visibility_result"]
    attribution_result = args["attribution_result"]
    provider = args.get("provider", "gemini")
    timestamp = args.get("timestamp") or datetime.now(timezone.utc).isoformat(timespec="seconds")

    visibility_score = (visibility_result.get("scores") or {}).get("visibility_score", 0)
    attribution_score = (attribution_result.get("scores") or {}).get("visibility_score", 0)
    visibility_runs = visibility_result.get("raw_runs") or []
    attribution_runs = attribution_result.get("raw_runs") or []

    merchant_host = _normalize_host(merchant_pdp_url)
    competitors, merchant_cited_runs, runs_with_any_citation = _extract_cited_hosts(
        attribution_runs, merchant_host=merchant_host,
    )

    verdict_label, verdict_explanation = _verdict_for(visibility_score, attribution_score)

    sections: List[str] = []
    sections.append(f"# AI Visibility Report — {merchant_name}\n")
    sections.append(
        f"_Generated {timestamp} · Provider: {provider} · Probe: pivota Demand Test Agent V1.5_\n"
    )

    sections.append("## Subject\n")
    bullets = [
        f"- **Merchant:** {merchant_name}",
        f"- **Verified URL:** {merchant_pdp_url}",
        f"- **Product tested:** {product_title}",
    ]
    if product_vendor:
        bullets.append(f"- **Vendor / brand:** {product_vendor}")
    if product_type:
        bullets.append(f"- **Category:** {product_type}")
    sections.append("\n".join(bullets) + "\n")

    sections.append(f"## Verdict: **{verdict_label}**\n")
    sections.append(verdict_explanation + "\n")
    sections.append(
        f"- **AI visibility score:** **{visibility_score}/100**  "
        f"(does Gemini surface this product when asked natural buyer queries?)\n"
        f"- **Direct attribution score:** **{attribution_score}/100**  "
        f"(when it does surface the product, does Gemini cite the merchant's own URL?)\n"
    )

    sections.append("## 1. Open product visibility\n")
    sections.append(
        f"We fed Gemini {len(visibility_runs)} buyer-style queries (auto-generated from "
        f"the product title + vendor + category). For each, we asked: did Gemini surface "
        f"the product as one of the answers?\n"
    )
    sections.append(_format_query_table(visibility_runs, "product_visible") + "\n")

    sections.append("## 2. Direct attribution\n")
    sections.append(
        f"We fed Gemini {len(attribution_runs)} buyer-style queries that should naturally "
        f"cite the merchant's own store as a buying path. For each, we asked: did Gemini "
        f"cite the verified merchant URL `{merchant_pdp_url}` "
        f"as a source (via Google Search grounding)?\n"
    )
    sections.append(_format_query_table(attribution_runs, "merchant_url_found") + "\n")

    sections.append("### Where AI shopping traffic is going instead\n")
    if runs_with_any_citation == 0:
        sections.append(
            "_(Gemini didn't return any cited URLs in its grounded answers. This usually "
            "means the product or product type is too long-tail for live web search to "
            "find anything — a stronger signal that the merchant is invisible to the "
            "AI-search channel than even a low attribution score.)_\n"
        )
    else:
        sections.append(
            f"Across {len(attribution_runs)} attribution queries, "
            f"Gemini's grounded search cited URLs from {runs_with_any_citation} runs. "
            f"The merchant's own URL appeared in {merchant_cited_runs} of those.\n"
        )
        sections.append("**Top cited competitor / third-party hosts:**\n")
        sections.append(_format_competitor_table(competitors) + "\n")
        if merchant_cited_runs == 0 and competitors:
            sections.append(
                "> Every grounded citation went to a third party. The merchant has "
                "_zero_ direct AI-channel attribution today.\n"
            )

    sections.append("## What this means for the merchant\n")
    sections.append(verdict_explanation + "\n")

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
        "or in the prose of the answer. The LLM's self-report is captured in `parsed` "
        "for transparency but does NOT drive the score (model frequently hallucinates "
        "self-attribution).\n"
        "- **Sample size:** 3 runs per scan_mode (conservative default; can be "
        "increased per probe call once worker-pool isolation lands upstream — see "
        "incident #280 for context).\n"
    )

    sections.append("## Raw probe data\n")
    sections.append(
        "<details><summary>Click to expand</summary>\n\n```json\n"
        + json.dumps(
            {
                "visibility": visibility_result,
                "attribution": attribution_result,
            },
            indent=2,
            default=str,
        )
        + "\n```\n</details>\n"
    )

    return "\n".join(sections)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


async def _main_async(args: argparse.Namespace) -> int:
    base_context: Dict[str, Any] = {
        "queries": [],
        "product": {
            "title": args.product_title,
            "vendor": args.product_vendor or None,
            "product_type": args.product_type or None,
        },
        "merchant_pdp_url": args.merchant_pdp_url,
    }

    print(
        f"Running open_product_visibility_test for {args.merchant_name}...",
        file=sys.stderr,
    )
    visibility_result = await _run_probe(
        scan_mode="open_product_visibility_test",
        merchant_name=args.merchant_name,
        context=base_context,
        provider=args.provider,
        max_runs=args.max_runs,
    )

    print(
        f"Running merchant_store_attribution_test for {args.merchant_name}...",
        file=sys.stderr,
    )
    attribution_result = await _run_probe(
        scan_mode="merchant_store_attribution_test",
        merchant_name=args.merchant_name,
        context=base_context,
        provider=args.provider,
        max_runs=args.max_runs,
    )

    if args.format == "json":
        out = {
            "merchant_name": args.merchant_name,
            "merchant_pdp_url": args.merchant_pdp_url,
            "product": base_context["product"],
            "provider": args.provider,
            "visibility": visibility_result,
            "attribution": attribution_result,
        }
        rendered = json.dumps(out, indent=2, default=str)
    else:
        rendered = render_markdown_report({
            "merchant_name": args.merchant_name,
            "merchant_pdp_url": args.merchant_pdp_url,
            "product_title": args.product_title,
            "product_vendor": args.product_vendor,
            "product_type": args.product_type,
            "visibility_result": visibility_result,
            "attribution_result": attribution_result,
            "provider": args.provider,
        })

    if args.output:
        os.makedirs(os.path.dirname(os.path.abspath(args.output)) or ".", exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(rendered)
        print(f"Report written to {args.output}", file=sys.stderr)
    else:
        print(rendered)

    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="BD AI-visibility report for an external (not-yet-onboarded) merchant.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Usage:")[1] if "Usage:" in __doc__ else "",
    )
    p.add_argument("--merchant-name", required=True,
                   help="Display name of the external merchant (e.g. 'Glossier')")
    p.add_argument("--merchant-pdp-url", required=True,
                   help="Verified canonical URL of the merchant's own PDP for the product")
    p.add_argument("--product-title", required=True)
    p.add_argument("--product-vendor", default=None,
                   help="Vendor / brand name (often same as merchant for D2C, different for retailers)")
    p.add_argument("--product-type", default=None,
                   help="Category (e.g. 'serum', 'blush', 'sneaker')")
    p.add_argument("--provider", default="gemini", choices=["mock", "gemini"],
                   help="Default 'gemini'. Use 'mock' to validate report wiring without burning Gemini quota.")
    p.add_argument("--max-runs", type=int, default=3,
                   help="Number of Gemini calls per scan_mode. Default 3 (post-#280 conservative). "
                        "~25k tokens per call.")
    p.add_argument("--format", default="markdown", choices=["markdown", "json"],
                   help="Output format. Markdown is BD-ready, JSON is for tooling.")
    p.add_argument("--output", default=None,
                   help="File path to write the report. Default: stdout.")
    return p


def main() -> int:
    args = build_parser().parse_args()
    try:
        return asyncio.run(_main_async(args))
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 — top-level CLI catch
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
