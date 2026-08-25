#!/usr/bin/env python3
"""
Pivota PDP self-baseline — runs visibility + attribution probes against
each canonical Pivota PDP (the seed set hardcoded into the sitemap) and
aggregates into a single report.

Two values:

1. **Operational health check** — surfaces which Pivota PDPs are actually
   indexed by Google + cited by Gemini grounding. Reads as a punch list:
   "PDP X is invisible to Gemini → submit URL Inspection to Search Console".

2. **BD pitch baseline** — the aggregate becomes the "after onboarding"
   reference we hand merchants. "Pivota's canonical PDPs currently get
   cited in X% of grounded Gemini answers; merchants in your category
   landing on Pivota PDPs see this baseline". Closes the BD pitch loop:
   without it, the rep can claim "onboarding fixes things" but has no
   evidence; with it, they have a real data point.

Usage:

  # Runs LOCALLY against the public gateway. Point it at PRODUCTION, which is
  # Cloud Run behind gateway.pivota.cc. The old Railway host still answers 200
  # and still serves a DIFFERENT build (gateway 74c655ee vs Railway c151f5f8,
  # measured 2026-08-25), so pointing here at the rollback yields a baseline that
  # looks valid and describes a platform nobody is served from.
  PROMOTIONS_ADMIN_KEY=...   \\
  PIVOTA_AGENT_INTERNAL_URL=https://gateway.pivota.cc \\
  python scripts/agent_center_pivota_pdp_baseline.py \\
    --output reports/pivota-pdp-baseline-2026-05-06.md

Reuses `services.agent_center_bd_report_service.run_bd_probes` so the
prompt / scoring layer stays aligned with the BD report. Each seed PDP
runs the same `open_product_visibility_test` + `merchant_store_attribution_test`
pair the BD report uses; we treat the Pivota PDP URL as the "merchant URL"
for scoring purposes.

Cost: ~6 grounded Gemini calls per seed × N seeds = ~36 calls / ~900k
tokens for the current 6-seed list. Run sparingly (operator-triggered,
not on a tight schedule). Can be lowered with --max-runs=2 for cheaper
sanity checks.

Exit codes:
  0  — report written successfully (or printed)
  1  — probe failed (network / upstream error)
  2  — invalid arguments / unrecoverable config error
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple


# Mirror of `pivota-agent-ui/src/app/sitemap-seeds.ts:SITEMAP_SEED_PRODUCT_IDS`.
# Kept as a hardcoded duplicate here (NOT fetched live from /api/gateway)
# so the script's value is a stable-baseline measurement: same seed set
# every run → trends are comparable across weeks. When the sitemap seed
# list changes, update this list too.
PIVOTA_PDP_SEEDS: List[Dict[str, str]] = [
    {
        "sig_id": "sig_7ad40676c42fb9c96e2a8136",
        "product_title": "Multi-Peptide Lash and Brow Serum",
        "product_vendor": "the ordinary",
        "product_type": "serum",
    },
    {
        "sig_id": "sig_7ed140c61dfa79d1c2876a7a",
        "product_title": "Eau d'Ombré Leather Eau de Toilette",
        "product_vendor": "Tom Ford Beauty",
        "product_type": "fragrance",
    },
    {
        "sig_id": "sig_7d3a5ec03e4e70ce239eaa0c",
        "product_title": "Unseen Sunscreen SPF 50",
        "product_vendor": "Supergoop!",
        "product_type": "sunscreen",
    },
    {
        "sig_id": "sig_29ed2e5f318a5d70a2f645ed",
        "product_title": "barrier restore cream",
        "product_vendor": "rhode",
        "product_type": "moisturizer",
    },
    {
        "sig_id": "sig_d89c869821249a14d3edbf25",
        "product_title": "Poreless Clarifying Charcoal Mask Pink",
        "product_vendor": "COSRX",
        "product_type": "mask",
    },
    {
        "sig_id": "sig_dacaf022d6c6a9ed86ecab1f",
        "product_title": "Revive Under Eye Patch: Ginseng + Retinal",
        "product_vendor": "Beauty of Joseon",
        "product_type": "eye patch",
    },
]

PIVOTA_PDP_URL_PREFIX = "https://agent.pivota.cc/products/"


def _pdp_url_for(sig_id: str) -> str:
    return f"{PIVOTA_PDP_URL_PREFIX}{sig_id}"


# ---------------------------------------------------------------------------
# Aggregation — pure logic for tests
# ---------------------------------------------------------------------------


def aggregate_per_pdp_results(
    per_pdp: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Walk a list of per-PDP structured BD reports and return the
    aggregated baseline numbers + diagnostics. Pure function.

    Returns:
      {
        seeds_count, succeeded_count, failed_count,
        median_visibility, median_attribution,
        cited_count            (PDPs cited at least once by Gemini grounding),
        not_cited_seeds        (sig_ids that got zero grounding cites — ops fix list),
        per_pdp_summary        (compact rows for the markdown table),
      }
    """
    succeeded = [r for r in per_pdp if r.get("status") == "ok"]
    failed = [r for r in per_pdp if r.get("status") != "ok"]

    visibility_scores = [
        r["report"]["verdict"]["visibility_score"]
        for r in succeeded
        if r.get("report")
    ]
    attribution_scores = [
        r["report"]["verdict"]["attribution_score"]
        for r in succeeded
        if r.get("report")
    ]

    not_cited_seeds: List[str] = []
    per_pdp_summary: List[Dict[str, Any]] = []
    for r in succeeded:
        report = r.get("report") or {}
        seed = r.get("seed", {})
        attribution = report.get("attribution") or {}
        merchant_cited = attribution.get("merchant_cited_runs", 0)
        runs_with_any = attribution.get("runs_with_any_citation", 0)
        if merchant_cited == 0:
            not_cited_seeds.append(seed.get("sig_id", "?"))
        per_pdp_summary.append({
            "sig_id": seed.get("sig_id"),
            "product_title": seed.get("product_title"),
            "product_vendor": seed.get("product_vendor"),
            "product_type": seed.get("product_type"),
            "verdict": (report.get("verdict") or {}).get("label"),
            "visibility_score": (report.get("verdict") or {}).get("visibility_score"),
            "attribution_score": (report.get("verdict") or {}).get("attribution_score"),
            "merchant_cited_runs": merchant_cited,
            "runs_with_any_citation": runs_with_any,
        })

    cited_count = sum(
        1 for r in succeeded
        if (r.get("report") or {}).get("attribution", {}).get("merchant_cited_runs", 0) > 0
    )
    return {
        "seeds_count": len(per_pdp),
        "succeeded_count": len(succeeded),
        "failed_count": len(failed),
        "median_visibility": _median(visibility_scores),
        "median_attribution": _median(attribution_scores),
        "cited_count": cited_count,
        "not_cited_seeds": not_cited_seeds,
        "per_pdp_summary": per_pdp_summary,
        "failed": [
            {"sig_id": (r.get("seed") or {}).get("sig_id"), "error": r.get("error")}
            for r in failed
        ],
    }


def _median(values: List[float]) -> Optional[float]:
    if not values:
        return None
    sorted_values = sorted(values)
    n = len(sorted_values)
    if n % 2 == 1:
        return float(sorted_values[n // 2])
    return (sorted_values[n // 2 - 1] + sorted_values[n // 2]) / 2.0


# ---------------------------------------------------------------------------
# Markdown report
# ---------------------------------------------------------------------------


def render_markdown(
    aggregate: Dict[str, Any],
    *,
    timestamp: str,
    provider: str,
    max_runs: int,
) -> str:
    parts: List[str] = []
    parts.append("# Pivota PDP Self-Baseline Report\n")
    parts.append(
        f"_Generated {timestamp} · Provider: `{provider}` · max_runs={max_runs} · "
        f"Probe: pivota Demand Test Agent V1.5_\n"
    )

    succeeded = aggregate["succeeded_count"]
    seeds = aggregate["seeds_count"]
    cited = aggregate["cited_count"]
    not_cited = aggregate["not_cited_seeds"]
    parts.append("## Operational health\n")
    parts.append(
        f"- Seeds tested: **{seeds}**\n"
        f"- Probes succeeded: **{succeeded}** / {seeds}\n"
        f"- PDPs cited by Gemini grounding (at least once): **{cited}** / {succeeded}\n"
    )
    if not_cited:
        parts.append(
            "\n### Action — Pivota PDPs that got zero grounded citations\n"
            "Submit URL Inspection in Google Search Console for each, then re-run this baseline in 72h.\n"
        )
        for sig_id in not_cited:
            parts.append(f"- `{_pdp_url_for(sig_id)}`")
        parts.append("\n")

    failed = aggregate.get("failed") or []
    if failed:
        parts.append("\n### Probes that failed (upstream error)\n")
        for f in failed:
            parts.append(f"- `{f.get('sig_id')}`: {f.get('error')}")
        parts.append("\n")

    parts.append("## BD-pitch baseline\n")
    median_vis = aggregate.get("median_visibility")
    median_attr = aggregate.get("median_attribution")
    if median_vis is not None and median_attr is not None:
        parts.append(
            f"Across {succeeded} canonical Pivota PDPs, the median **visibility "
            f"score is {median_vis:.0f}/100** and the median **attribution score "
            f"is {median_attr:.0f}/100**. Hand this to BD as the realistic "
            f"\"after onboarding\" baseline for category-comparable products: "
            f"a merchant onboarding Pivota lands on canonical PDPs that today "
            f"score in this range.\n"
        )
    else:
        parts.append("_(no successful probes — can't aggregate; fix probe wiring first.)_\n")

    parts.append("## Per-PDP detail\n")
    rows = aggregate.get("per_pdp_summary") or []
    if not rows:
        parts.append("_(no successful probes)_\n")
    else:
        parts.append(
            "| PDP | Vendor | Verdict | Visibility | Attribution | Merchant cited |\n"
            "|---|---|---|---|---|---|"
        )
        for row in rows:
            sig = row.get("sig_id") or "?"
            title = (row.get("product_title") or "")[:40]
            vendor = row.get("product_vendor") or ""
            verdict = row.get("verdict") or "?"
            vis = row.get("visibility_score")
            att = row.get("attribution_score")
            cited_runs = row.get("merchant_cited_runs", 0)
            any_runs = row.get("runs_with_any_citation", 0)
            parts.append(
                f"| `{sig}`<br/>_{title}_ | {vendor} | {verdict} | "
                f"{vis if vis is not None else '?'}/100 | "
                f"{att if att is not None else '?'}/100 | "
                f"{cited_runs}/{any_runs} |"
            )
        parts.append("")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Probe orchestration
# ---------------------------------------------------------------------------


async def _run_one_seed(
    seed: Dict[str, str],
    *,
    provider: str,
    max_runs: int,
) -> Dict[str, Any]:
    """Run BD probes against ONE Pivota PDP. Returns an envelope with
    `status: 'ok' | 'error'` and `report` or `error`. Never raises —
    individual seed failures shouldn't abort the whole baseline run."""
    from services.agent_center_bd_report_service import (
        build_structured_report,
        run_bd_probes,
    )

    pdp_url = _pdp_url_for(seed["sig_id"])
    try:
        probes = await run_bd_probes(
            merchant_name=f"Pivota Canonical — {seed['product_vendor']}",
            merchant_pdp_url=pdp_url,
            product_title=seed["product_title"],
            product_vendor=seed.get("product_vendor"),
            product_type=seed.get("product_type"),
            provider=provider,
            max_runs=max_runs,
        )
        report = build_structured_report(
            merchant_name=f"Pivota Canonical — {seed['product_vendor']}",
            merchant_pdp_url=pdp_url,
            product_title=seed["product_title"],
            product_vendor=seed.get("product_vendor"),
            product_type=seed.get("product_type"),
            visibility_result=probes["visibility"],
            attribution_result=probes["attribution"],
            category_visibility_result=probes.get("category_visibility"),
            provider=provider,
        )
        return {"status": "ok", "seed": seed, "report": report}
    except Exception as exc:  # noqa: BLE001 — operator script, log and continue
        return {"status": "error", "seed": seed, "error": str(exc)}


async def _main_async(args: argparse.Namespace) -> int:
    seeds = PIVOTA_PDP_SEEDS
    if args.limit and args.limit > 0:
        seeds = seeds[: args.limit]

    print(
        f"Running Pivota PDP self-baseline against {len(seeds)} seeds "
        f"(provider={args.provider}, max_runs={args.max_runs})...",
        file=sys.stderr,
    )

    per_pdp: List[Dict[str, Any]] = []
    for idx, seed in enumerate(seeds, start=1):
        print(
            f"  [{idx}/{len(seeds)}] {seed['sig_id']} ({seed['product_vendor']})...",
            file=sys.stderr,
        )
        result = await _run_one_seed(
            seed, provider=args.provider, max_runs=args.max_runs,
        )
        if result.get("status") == "error":
            print(f"    ✗ {result.get('error')}", file=sys.stderr)
        per_pdp.append(result)

    aggregate = aggregate_per_pdp_results(per_pdp)
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")

    if args.format == "json":
        rendered = json.dumps(
            {
                "timestamp": timestamp,
                "provider": args.provider,
                "max_runs": args.max_runs,
                "aggregate": aggregate,
                "per_pdp": per_pdp,
            },
            indent=2,
            default=str,
        )
    else:
        rendered = render_markdown(
            aggregate,
            timestamp=timestamp,
            provider=args.provider,
            max_runs=args.max_runs,
        )

    if args.output:
        os.makedirs(os.path.dirname(os.path.abspath(args.output)) or ".", exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(rendered)
        print(f"Report written to {args.output}", file=sys.stderr)
    else:
        print(rendered)

    # Exit non-zero if every seed failed (signal for ops cron jobs).
    return 0 if aggregate["succeeded_count"] > 0 else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Pivota PDP self-baseline — health check + BD pitch reference",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Usage:")[1] if "Usage:" in __doc__ else "",
    )
    p.add_argument("--provider", default="gemini", choices=["mock", "gemini"],
                   help="Default 'gemini'. 'mock' validates wiring without burning quota.")
    p.add_argument("--max-runs", type=int, default=3,
                   help="Number of Gemini calls per scan_mode per seed. Default 3 "
                        "(post-#280 conservative).")
    p.add_argument("--format", default="markdown", choices=["markdown", "json"],
                   help="Output format. Markdown is operator-friendly, JSON for tooling.")
    p.add_argument("--output", default=None,
                   help="File path to write the report. Default: stdout.")
    p.add_argument("--limit", type=int, default=None,
                   help="Test only the first N seeds (for fast smoke runs).")
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
        return 2


if __name__ == "__main__":
    sys.exit(main())
