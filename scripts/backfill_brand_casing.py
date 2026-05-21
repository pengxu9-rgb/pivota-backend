#!/usr/bin/env python3
"""
Backfill catalog_products.brand from lowercase to proper display case.

Targets rows where `brand` is fully lowercase (e.g. "fenty beauty") —
the result of upstream ingest paths that lowercased the candidate brand
field. The matching `pivota-agent` PDP composer now also Title-Cases at
read time (see `src/pdpBuilder.js::titleCaseBrand`), so this backfill is
defense-in-depth + cleanup of the stored value.

Identity/dedup remains safe: services.catalog_identity.normalize_brand
lowercases its input for matching, so changing the displayed case has no
effect on canonical_product_name / content_key collisions.

Default is dry-run. Pass --apply to UPDATE.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db.database import database
from services.text_normalization.brand_case import proper_case_brand


SELECT_LOWERCASE_ROWS_SQL = """
SELECT product_key, brand
FROM catalog_products
WHERE brand IS NOT NULL
  AND brand = lower(brand)
  AND brand <> upper(brand)
ORDER BY brand, product_key
{limit_clause}
"""


UPDATE_SQL = """
UPDATE catalog_products
SET brand = :new_brand, updated_at = now()
WHERE product_key = :product_key
  AND brand = :old_brand
"""


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="UPDATE rows. Default is dry-run.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Limit rows touched (0 = all matching).",
    )
    parser.add_argument(
        "--sample-limit",
        type=int,
        default=30,
        help="Number of sample before/after pairs to include in report.",
    )
    parser.add_argument("--output-json", default=None)
    parser.add_argument("--output-md", default=None)
    return parser.parse_args()


def _write_if_requested(path_str: Optional[str], content: str) -> None:
    if not path_str:
        return
    path = Path(path_str)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


async def _fetch_candidates(limit: int) -> List[Dict[str, Any]]:
    limit_clause = f"LIMIT {int(limit)}" if limit and limit > 0 else ""
    sql = SELECT_LOWERCASE_ROWS_SQL.format(limit_clause=limit_clause)
    rows = await database.fetch_all(sql)
    return [dict(r) for r in (rows or [])]


def _plan_updates(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    plan: List[Dict[str, Any]] = []
    for row in rows:
        old = row.get("brand")
        new = proper_case_brand(old)
        if not new or new == old:
            continue
        plan.append(
            {
                "product_key": row["product_key"],
                "old_brand": old,
                "new_brand": new,
            }
        )
    return plan


async def _apply_updates(plan: List[Dict[str, Any]]) -> int:
    updated = 0
    for item in plan:
        result = await database.execute(
            UPDATE_SQL,
            {
                "new_brand": item["new_brand"],
                "old_brand": item["old_brand"],
                "product_key": item["product_key"],
            },
        )
        # `databases` returns the row id for INSERT; for UPDATE the count
        # is not always returned uniformly across backends. Count attempts
        # as updates — the WHERE clause guards against double-application.
        updated += 1
        if result is None:
            continue
    return updated


def _summarize(plan: List[Dict[str, Any]]) -> Dict[str, Any]:
    by_brand: Dict[str, int] = {}
    for item in plan:
        by_brand[item["new_brand"]] = by_brand.get(item["new_brand"], 0) + 1
    return {
        "candidates": len(plan),
        "unique_brands_after": len(by_brand),
        "top_brands": sorted(by_brand.items(), key=lambda kv: -kv[1])[:20],
    }


def _render_markdown(report: Dict[str, Any]) -> str:
    lines = [
        "# Catalog Products Brand Casing Backfill",
        "",
        f"- generated_at: `{report['generated_at']}`",
        f"- apply: `{report['apply']}`",
        f"- candidates: `{report['summary']['candidates']}`",
        f"- updates_applied: `{report.get('updates_applied', 0)}`",
        f"- unique_brands_after: `{report['summary']['unique_brands_after']}`",
        "",
        "## Top brands by row count (post-fix)",
        "",
    ]
    for brand, n in report["summary"]["top_brands"]:
        lines.append(f"- `{brand}`: {n}")
    lines.append("")
    lines.append("## Sample before → after")
    lines.append("")
    lines.append("| product_key | before | after |")
    lines.append("|---|---|---|")
    for sample in report.get("sample", []):
        lines.append(
            f"| `{sample['product_key']}` | `{sample['old_brand']}` | `{sample['new_brand']}` |"
        )
    return "\n".join(lines) + "\n"


async def _run(args: argparse.Namespace) -> Dict[str, Any]:
    await database.connect()
    try:
        rows = await _fetch_candidates(args.limit)
        plan = _plan_updates(rows)
        report: Dict[str, Any] = {
            "ok": True,
            "apply": args.apply,
            "limit": args.limit,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "summary": _summarize(plan),
            "sample": plan[: args.sample_limit],
        }
        if args.apply:
            updated = await _apply_updates(plan)
            report["updates_applied"] = updated
        else:
            report["updates_applied"] = 0
        return report
    finally:
        await database.disconnect()


def main() -> int:
    args = _parse_args()
    report = asyncio.run(_run(args))
    json_blob = json.dumps(report, indent=2, default=str)
    md_blob = _render_markdown(report)
    print(json_blob)
    _write_if_requested(args.output_json, json_blob)
    _write_if_requested(args.output_md, md_blob)
    return 0


if __name__ == "__main__":
    sys.exit(main())
