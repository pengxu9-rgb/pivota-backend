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

This CLI is a thin wrapper around `services.agent_center_bd_report_service`
— same code path the employee-portal BD report UI uses, so terminal
runs and UI runs produce identical output.

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

Defaults: provider=gemini, max_runs=3 (post-#280 conservative default;
bump only after worker-pool isolation lands upstream — see
feedback_llm_call_multipliers.md). Each probe = ~3 grounded Gemini
calls × ~25k tokens. One BD report = ~150k tokens.

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
from typing import Any, Dict

# Shared service — same code path the BD report HTTP route uses.
from services.agent_center_bd_report_service import (
    build_structured_report,
    extract_cited_hosts,
    normalize_host,
    render_markdown_from_structured,
    run_bd_probes,
    verdict_for,
)

# Re-exports kept for backward compatibility with existing tests in
# `tests/test_agent_center_bd_external_merchant.py`. New tests should
# import from `services.agent_center_bd_report_service` directly.
_normalize_host = normalize_host
_extract_cited_hosts = extract_cited_hosts
_verdict_for = verdict_for


def render_markdown_report(args: Dict[str, Any]) -> str:
    """Backward-compat shim: existing tests pass the same shape that
    `build_structured_report` accepts via kwargs. Build the structured
    report and render it to markdown."""
    structured = build_structured_report(
        merchant_name=args["merchant_name"],
        merchant_pdp_url=args["merchant_pdp_url"],
        product_title=args["product_title"],
        product_vendor=args.get("product_vendor"),
        product_type=args.get("product_type"),
        visibility_result=args["visibility_result"],
        attribution_result=args["attribution_result"],
        provider=args.get("provider", "gemini"),
        timestamp=args.get("timestamp"),
    )
    return render_markdown_from_structured(structured)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


async def _main_async(args: argparse.Namespace) -> int:
    print(
        f"Running BD probes for {args.merchant_name}...",
        file=sys.stderr,
    )
    probes = await run_bd_probes(
        merchant_name=args.merchant_name,
        merchant_pdp_url=args.merchant_pdp_url,
        product_title=args.product_title,
        product_vendor=args.product_vendor,
        product_type=args.product_type,
        provider=args.provider,
        max_runs=args.max_runs,
    )

    structured = build_structured_report(
        merchant_name=args.merchant_name,
        merchant_pdp_url=args.merchant_pdp_url,
        product_title=args.product_title,
        product_vendor=args.product_vendor,
        product_type=args.product_type,
        visibility_result=probes["visibility"],
        attribution_result=probes["attribution"],
        provider=args.provider,
    )

    if args.format == "json":
        rendered = json.dumps(structured, indent=2, default=str)
    else:
        rendered = render_markdown_from_structured(structured)

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
