#!/usr/bin/env python3
"""Per-root category distribution across catalog_products.

Read-only report. Answers the question "which top-level e-commerce
categories do real Pivota merchants actually have products in?" so
the team can prioritize which categories get a merchant-authoring
surface next.

The merchant agent surface today covers fashion, beauty/skincare-shape,
and beauty/tools. The classifier knows about 11 top-level roots
(fashion, beauty, electronics, home, outdoor, sports, toys, pet, food,
books, other). Products in non-covered roots classify correctly but
get NO downstream extraction or merchant authoring path. This script
quantifies that gap so we know which root to ship next.

Pairs with `docs/CATEGORY_AUTHORING_EXTENSION.md` — once a root crosses
the signal threshold (≥1 merchant with >20 products, OR ≥3 merchants
in the root), the runbook tells you how to ship its authoring surface
in ~half a day.

No writes. Safe to run against prod.

Usage:
  python scripts/report_category_distribution.py
  python scripts/report_category_distribution.py --merchant-id merch_xxx
  python scripts/report_category_distribution.py --format md
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db.database import database  # noqa: E402


# Roots the merchant agent surface (and per-category authoring service)
# covers today. Anything outside this set is a candidate for v2.2+.
_COVERED_ROOTS = {"fashion", "beauty"}


async def _root_distribution(*, merchant_id: Optional[str]) -> List[Dict[str, Any]]:
    """One row per top-level root: products, distinct merchants, +
    classifier-source breakdown so we can see how confident the
    classification is."""
    where_merchant = "AND cp.merchant_id = :merchant_id" if merchant_id else ""
    sql = f"""
        SELECT
          SPLIT_PART(cp.category_path, '/', 1) AS root,
          COUNT(*) AS products,
          COUNT(DISTINCT cp.merchant_id) AS merchants,
          COUNT(*) FILTER (
            WHERE cp.category_label_source = 'regex_backfill'
              OR cp.category_label_source = 'merchant_payload'
          ) AS regex_or_payload,
          COUNT(*) FILTER (WHERE cp.category_label_source = 'llm_category_v1') AS llm_classified,
          COUNT(*) FILTER (
            WHERE cp.category_label_source IS NULL
              OR cp.category_label_source NOT IN (
                'regex_backfill', 'merchant_payload', 'llm_category_v1'
              )
          ) AS unknown_source,
          ROUND(AVG(cp.category_confidence)::numeric, 3) AS avg_confidence
        FROM catalog_products cp
        WHERE cp.category_path IS NOT NULL
          AND cp.category_path <> ''
          {where_merchant}
        GROUP BY root
        ORDER BY products DESC
    """
    rows = await database.fetch_all(
        sql, {"merchant_id": merchant_id} if merchant_id else {}
    )
    return [dict(r) for r in rows or []]


async def _unclassified(*, merchant_id: Optional[str]) -> Dict[str, int]:
    """The silent-failure case: products with NULL category_path. They
    didn't match any regex and either didn't go through the LLM
    fallback or the LLM declined."""
    where_merchant = "AND cp.merchant_id = :merchant_id" if merchant_id else ""
    sql = f"""
        SELECT
          COUNT(*) AS products,
          COUNT(DISTINCT cp.merchant_id) AS merchants
        FROM catalog_products cp
        WHERE (cp.category_path IS NULL OR cp.category_path = '')
          {where_merchant}
    """
    row = await database.fetch_one(
        sql, {"merchant_id": merchant_id} if merchant_id else {}
    )
    return dict(row) if row else {"products": 0, "merchants": 0}


async def _per_merchant_top_roots(*, merchant_id: Optional[str], limit: int = 20) -> List[Dict[str, Any]]:
    """For the report's per-merchant view, list (merchant, root, products)
    so we can see catalogs that are concentrated in a single root vs
    broadly spread. Most useful when running global (no --merchant-id)
    to spot "a single merchant has 1000 electronics products"."""
    where_merchant = "AND cp.merchant_id = :merchant_id" if merchant_id else ""
    sql = f"""
        SELECT
          cp.merchant_id,
          SPLIT_PART(cp.category_path, '/', 1) AS root,
          COUNT(*) AS products
        FROM catalog_products cp
        WHERE cp.category_path IS NOT NULL
          AND cp.category_path <> ''
          {where_merchant}
        GROUP BY cp.merchant_id, root
        ORDER BY products DESC
        LIMIT :limit
    """
    params: Dict[str, Any] = {"limit": limit}
    if merchant_id:
        params["merchant_id"] = merchant_id
    rows = await database.fetch_all(sql, params)
    return [dict(r) for r in rows or []]


async def build_report(*, merchant_id: Optional[str]) -> Dict[str, Any]:
    roots = await _root_distribution(merchant_id=merchant_id)
    unclassified = await _unclassified(merchant_id=merchant_id)
    per_merchant_top = await _per_merchant_top_roots(merchant_id=merchant_id)

    total_products = sum(r["products"] for r in roots) + int(unclassified["products"] or 0)
    covered_products = sum(r["products"] for r in roots if r["root"] in _COVERED_ROOTS)
    uncovered_products = total_products - covered_products - int(unclassified["products"] or 0)

    return {
        "scope": {"merchant_id": merchant_id} if merchant_id else {"merchant_id": None},
        "totals": {
            "total_classified_products": sum(r["products"] for r in roots),
            "total_unclassified_products": int(unclassified["products"] or 0),
            "covered_root_products": covered_products,
            "uncovered_root_products": uncovered_products,
        },
        "roots": [
            {
                **r,
                "covered_by_agent_surface": r["root"] in _COVERED_ROOTS,
            }
            for r in roots
        ],
        "unclassified": unclassified,
        "concentration_top_pairs": per_merchant_top,
    }


def _format_markdown(report: Dict[str, Any]) -> str:
    lines = ["# Category distribution", ""]
    t = report["totals"]
    lines.append(f"**Total classified products:** {t['total_classified_products']:,}")
    lines.append(f"**Total unclassified products (NULL category_path):** {t['total_unclassified_products']:,}")
    lines.append(f"**Products in agent-covered roots (fashion + beauty):** {t['covered_root_products']:,}")
    lines.append(f"**Products in uncovered roots (the v2.2 candidate pool):** {t['uncovered_root_products']:,}")
    lines.append("")
    lines.append("## Per-root breakdown")
    lines.append("")
    lines.append("| Root | Products | Merchants | Avg confidence | Source: regex/payload | Source: LLM | Source: unknown | Agent surface |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|:---|")
    for row in report["roots"]:
        covered = "✅ covered" if row["covered_by_agent_surface"] else "⚠️ no agent surface"
        lines.append(
            f"| `{row['root']}` | {row['products']:,} | {row['merchants']} | "
            f"{row['avg_confidence'] or '—'} | {row['regex_or_payload']:,} | "
            f"{row['llm_classified']:,} | {row['unknown_source']:,} | {covered} |"
        )
    lines.append("")
    if report["unclassified"]["products"]:
        lines.append(
            f"**Unclassified rows (NULL category_path):** "
            f"{report['unclassified']['products']:,} products across "
            f"{report['unclassified']['merchants']} merchants. These bypass "
            f"the entire authoring surface — worth a separate triage."
        )
        lines.append("")
    lines.append("## Top (merchant, root) concentrations")
    lines.append("")
    lines.append("Top 20 by product count. Use this to spot a single merchant with a large catalog in a non-covered root (the strongest signal for prioritizing that root's authoring path).")
    lines.append("")
    lines.append("| Merchant | Root | Products |")
    lines.append("|---|---|---:|")
    for row in report["concentration_top_pairs"]:
        lines.append(f"| `{row['merchant_id']}` | `{row['root']}` | {row['products']:,} |")
    lines.append("")
    lines.append("## Signal thresholds (from `the-problem-here-is-curious-waterfall.md`)")
    lines.append("")
    lines.append("Build a category's authoring surface when any of:")
    lines.append("- ≥1 merchant has >20 products in a single non-covered root")
    lines.append("- ≥3 merchants in a single non-covered root (even with small catalogs)")
    lines.append("- A merchant explicitly asks for it")
    lines.append("")
    lines.append("The runbook for shipping a category is at `docs/CATEGORY_AUTHORING_EXTENSION.md`.")
    return "\n".join(lines)


async def _run(args: argparse.Namespace) -> int:
    if not getattr(database, "is_connected", False):
        await database.connect()
    try:
        report = await build_report(merchant_id=args.merchant_id)
        if args.format == "md":
            print(_format_markdown(report))
        else:
            print(json.dumps(report, indent=2, default=str))
        return 0
    finally:
        if getattr(database, "is_connected", False):
            await database.disconnect()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--merchant-id",
        default=None,
        help="Restrict the report to a single merchant_id. Default: all merchants.",
    )
    parser.add_argument(
        "--format",
        choices=("json", "md"),
        default="md",
        help="Output format. md = the human-readable summary (default); json = the structured report.",
    )
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
