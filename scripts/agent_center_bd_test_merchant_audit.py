#!/usr/bin/env python3
"""
BD test-merchant audit — runs the full AI Commerce Readiness audit
against the internal Shopify test merchant + writes the markdown to
`reports/test-merchant-bd-audit.md`.

This artifact is what the BD pitch references in the "discovery_lift"
section + each "test_merchant_validation" cell of the onboarding
sequence. Without this, those references are empty pointers; with it,
BD can hand the prospective merchant a paired audit run on a
real Pivota-onboarded merchant — the live operational reference for
"this is what your post-onboarding state looks like".

The CLI does NOT live-fetch the test merchant's catalog from the Shopify
admin API (that needs the OAuth token at runtime). Instead, the catalog
is hardcoded with 3-5 representative SKUs that match the test merchant's
known seed catalog. Refresh the SKU list when the test merchant's
catalog changes (rarely — it's a stable test fixture).

Usage:

  PROMOTIONS_ADMIN_KEY=...   \\
  PIVOTA_AGENT_INTERNAL_URL=https://pivota-agent-production.up.railway.app \\
  python scripts/agent_center_bd_test_merchant_audit.py \\
    --output reports/test-merchant-bd-audit.md

Reuses `services.agent_center_bd_report_service.run_bd_probes` +
`build_structured_report` + `render_markdown_from_structured` so the
audit shape stays in sync with the BD report. Each SKU runs all three
scan modes (`open_product_visibility_test` + `merchant_store_attribution_test`
+ `category_visibility_test`).

Cost: ~9 grounded Gemini calls per SKU × 3 SKUs (default) = ~27 calls /
~700k tokens for a full refresh. Run sparingly (operator-triggered,
~monthly). Bump max_runs to 1 with --max-runs=1 for cheaper sanity
checks.

Exit codes:
  0  — report written successfully (at least one SKU succeeded)
  1  — every SKU failed (network / upstream error)
  2  — invalid arguments / unrecoverable config error
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


# Pivota internal Shopify test merchant — same identity referenced in
# the BD report's onboarding_sequence. Single source of truth.
TEST_MERCHANT_ID = "merch_38fa56d5118b9974"
TEST_MERCHANT_SHOP_DOMAIN = "shop.myshopify.com"


# Test merchant catalog snapshot. Mirrors the SKUs the test merchant
# carries in its Shopify admin. Each entry is shaped to match the BD
# report's expected probe context (title + vendor + product_type +
# pdp_url). Keep this small (3-5 SKUs) so the audit cost stays bounded.
#
# Refresh by hand when the test merchant's catalog changes; the goal is
# a stable post-onboarding reference, not a live mirror of the storefront.
TEST_MERCHANT_CATALOG: List[Dict[str, str]] = [
    {
        "title": "Advanced Snail Mucin Glass Glow Hydrogel Mask",
        "vendor": "COSRX",
        "product_type": "face mask",
        "pdp_url": "https://shop.myshopify.com/products/cosrx-snail-mucin-mask",
    },
    {
        "title": "Multi-Peptide Lash and Brow Serum",
        "vendor": "The Ordinary",
        "product_type": "serum",
        "pdp_url": "https://shop.myshopify.com/products/the-ordinary-lash-brow-serum",
    },
    {
        "title": "Revive Under Eye Patch: Ginseng + Retinal",
        "vendor": "Beauty of Joseon",
        "product_type": "eye patch",
        "pdp_url": "https://shop.myshopify.com/products/boj-revive-eye-patch",
    },
]


async def _run_one_sku(
    sku: Dict[str, str],
    *,
    provider: str,
    max_runs: int,
) -> Dict[str, Any]:
    """Run BD probes against ONE test-merchant SKU. Returns an envelope
    with `status: 'ok' | 'error'` plus the structured report or the
    error string. Never raises — individual SKU failures shouldn't
    abort the full run."""
    from services.agent_center_bd_report_service import (
        build_structured_report,
        run_bd_probes,
    )

    try:
        probes = await run_bd_probes(
            merchant_name=f"Pivota test merchant ({sku['vendor']})",
            merchant_pdp_url=sku["pdp_url"],
            product_title=sku["title"],
            product_vendor=sku.get("vendor"),
            product_type=sku.get("product_type"),
            provider=provider,
            max_runs=max_runs,
        )
        report = build_structured_report(
            merchant_name=f"Pivota test merchant ({sku['vendor']})",
            merchant_pdp_url=sku["pdp_url"],
            product_title=sku["title"],
            product_vendor=sku.get("vendor"),
            product_type=sku.get("product_type"),
            visibility_result=probes["visibility"],
            attribution_result=probes["attribution"],
            category_visibility_result=probes.get("category_visibility"),
            provider=provider,
        )
        return {"status": "ok", "sku": sku, "report": report}
    except Exception as exc:  # noqa: BLE001 — operator script
        return {"status": "error", "sku": sku, "error": str(exc)}


def _aggregate_scores(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    succeeded = [r for r in results if r.get("status") == "ok"]
    failed = [r for r in results if r.get("status") != "ok"]
    if not succeeded:
        return {
            "skus_total": len(results),
            "skus_succeeded": 0,
            "skus_failed": len(failed),
            "median_visibility": None,
            "median_attribution": None,
            "median_category_visibility": None,
        }
    vis = sorted(
        r["report"]["verdict"]["visibility_score"] for r in succeeded
    )
    attr = sorted(
        r["report"]["verdict"]["attribution_score"] for r in succeeded
    )
    cat = sorted(
        r["report"]["verdict"].get("category_visibility_score")
        for r in succeeded
        if r["report"]["verdict"].get("category_visibility_score") is not None
    )

    def _median(xs: List[int]) -> Optional[float]:
        if not xs:
            return None
        n = len(xs)
        mid = n // 2
        return xs[mid] if n % 2 == 1 else (xs[mid - 1] + xs[mid]) / 2

    return {
        "skus_total": len(results),
        "skus_succeeded": len(succeeded),
        "skus_failed": len(failed),
        "median_visibility": _median(vis),
        "median_attribution": _median(attr),
        "median_category_visibility": _median(cat),
    }


def _render_markdown(
    *,
    timestamp: str,
    aggregate: Dict[str, Any],
    results: List[Dict[str, Any]],
    provider: str,
    max_runs: int,
) -> str:
    sections: List[str] = []
    sections.append(
        "# AI Commerce Readiness Reference — Pivota Internal Test Merchant\n"
    )
    sections.append(
        f"_Generated {timestamp} · Test merchant `{TEST_MERCHANT_ID}` "
        f"@ `{TEST_MERCHANT_SHOP_DOMAIN}` · Probe: pivota Demand Test "
        f"Agent V1.5 · Provider: {provider} (max_runs={max_runs})_\n"
    )
    sections.append(
        "This is the **post-onboarding reference audit** Pivota's BD "
        "team hands prospective merchants alongside their own AI "
        "Commerce Readiness Report. The test merchant is fully "
        "Pivota-onboarded (Shopify OAuth → ACP → order forwarding "
        "verified end-to-end via "
        "`pivota-acp/test_epic5_shopify_order_poc.sh`). The numbers "
        "below show what an onboarded merchant's AI-channel surface "
        "actually looks like — the live operational reference for the "
        "BD report's `discovery_lift.pivota_reference` figures.\n"
    )

    sections.append("## Aggregate scores\n")
    sections.append(
        f"- **SKUs audited:** {aggregate['skus_total']} "
        f"({aggregate['skus_succeeded']} succeeded, "
        f"{aggregate['skus_failed']} failed)\n"
        f"- **Median named-product visibility score:** "
        f"{aggregate['median_visibility'] if aggregate['median_visibility'] is not None else 'n/a'}/100\n"
        f"- **Median first-party attribution score:** "
        f"{aggregate['median_attribution'] if aggregate['median_attribution'] is not None else 'n/a'}/100\n"
        f"- **Median category-level visibility score:** "
        f"{aggregate['median_category_visibility'] if aggregate['median_category_visibility'] is not None else 'n/a'}/100\n"
    )

    sections.append("## Per-SKU summary\n")
    rows = ["| SKU | Vendor | Visibility | Attribution | Category | Verdict |", "|---|---|---|---|---|---|"]
    for r in results:
        sku = r.get("sku") or {}
        if r.get("status") != "ok":
            rows.append(
                f"| {sku.get('title', '?')} | {sku.get('vendor', '?')} "
                f"| ❌ error | ❌ error | ❌ error | _failed: "
                f"{r.get('error', 'unknown')[:60]}_ |"
            )
            continue
        v = (r.get("report") or {}).get("verdict") or {}
        rows.append(
            f"| {sku.get('title', '?')} "
            f"| {sku.get('vendor', '?')} "
            f"| {v.get('visibility_score', 'n/a')}/100 "
            f"| {v.get('attribution_score', 'n/a')}/100 "
            f"| {v.get('category_visibility_score', 'n/a') if v.get('category_visibility_score') is not None else 'n/a'}/100 "
            f"| {v.get('label', '?')} |"
        )
    sections.append("\n".join(rows) + "\n")

    sections.append("## Methodology\n")
    sections.append(
        "- **Provider:** Gemini 2.5 Flash with Google Search grounding "
        "(live web, not training data).\n"
        "- **Per-SKU probes:** `open_product_visibility_test` + "
        "`merchant_store_attribution_test` + `category_visibility_test`. "
        f"Each runs {max_runs} times.\n"
        "- **Test merchant:** Pivota-onboarded Shopify store (OAuth "
        "completed, access_token stored, order-forwarding verified). "
        "The `merchant_pdp_url` for each SKU is the test merchant's "
        "Shopify URL; visibility + attribution scoring uses the same "
        "rules as the prospective-merchant BD report.\n"
        "- **Refresh cadence:** Operator runs this script monthly via "
        "`python scripts/agent_center_bd_test_merchant_audit.py "
        "--output reports/test-merchant-bd-audit.md`. Commit the "
        "regenerated artifact so BD has a stable URL for the pitch.\n"
    )
    sections.append(
        "_How to refresh:_ Set `PROMOTIONS_ADMIN_KEY` and "
        "`PIVOTA_AGENT_INTERNAL_URL`, then run the script. Cost is "
        "~9 grounded Gemini calls per SKU × "
        f"{len(TEST_MERCHANT_CATALOG)} SKUs ≈ "
        f"{9 * len(TEST_MERCHANT_CATALOG)} calls per refresh.\n"
    )

    return "\n".join(sections)


async def _main_async(args: argparse.Namespace) -> int:
    skus = TEST_MERCHANT_CATALOG
    if args.limit and args.limit > 0:
        skus = skus[: args.limit]

    print(
        f"Running BD test-merchant audit against {len(skus)} SKUs "
        f"(provider={args.provider}, max_runs={args.max_runs})...",
        file=sys.stderr,
    )

    results: List[Dict[str, Any]] = []
    for idx, sku in enumerate(skus, start=1):
        print(
            f"  [{idx}/{len(skus)}] {sku['title']} ({sku['vendor']})...",
            file=sys.stderr,
        )
        r = await _run_one_sku(
            sku, provider=args.provider, max_runs=args.max_runs,
        )
        if r.get("status") == "error":
            print(f"    ✗ {r.get('error')}", file=sys.stderr)
        results.append(r)

    aggregate = _aggregate_scores(results)
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")

    rendered = _render_markdown(
        timestamp=timestamp,
        aggregate=aggregate,
        results=results,
        provider=args.provider,
        max_runs=args.max_runs,
    )

    if args.output:
        os.makedirs(
            os.path.dirname(os.path.abspath(args.output)) or ".",
            exist_ok=True,
        )
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(rendered)
        print(f"Audit written to {args.output}", file=sys.stderr)
    else:
        print(rendered)

    return 0 if aggregate["skus_succeeded"] > 0 else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "BD test-merchant audit — produces the post-onboarding "
            "reference artifact for the BD report"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            __doc__.split("Usage:")[1] if "Usage:" in (__doc__ or "") else ""
        ),
    )
    p.add_argument(
        "--provider", default="gemini", choices=["mock", "gemini"],
        help="Default 'gemini'. 'mock' validates wiring without burning quota.",
    )
    p.add_argument(
        "--max-runs", type=int, default=3,
        help="Number of Gemini calls per scan_mode per SKU. Default 3 "
             "(post-#280 conservative).",
    )
    p.add_argument(
        "--output", default=None,
        help="File path to write the report. Default: stdout.",
    )
    p.add_argument(
        "--limit", type=int, default=None,
        help="Audit only the first N SKUs (for fast smoke runs).",
    )
    return p


def main() -> int:
    args = build_parser().parse_args()
    try:
        return asyncio.run(_main_async(args))
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 — top-level CLI
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
