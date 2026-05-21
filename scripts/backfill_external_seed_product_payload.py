#!/usr/bin/env python3
"""
Backfill catalog_products.product_payload for external_seed rows so the
force-filled PDP content (pdp_details_sections, pdp_how_to_use_raw,
pdp_faq_items, ingredient_intel, etc.) is surfaced at the top level
where the PIVOTA-Agent PDP composer reads it.

The mirror script (scripts/mirror_external_seeds_to_catalog_products.py)
uses ON CONFLICT DO NOTHING, so updates to its product_payload shape
only apply to NEW rows. This script heals the 4,500+ existing rows.

The rich seed content is already embedded in product_payload.seed_data;
this script just lifts those keys to top-level and adds a structured
product_payload.brand object so the read path finds it.

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


# Keep in sync with scripts/mirror_external_seeds_to_catalog_products.py
RICH_KEYS = (
    "pdp_description_raw",
    "pdp_details_sections",
    "pdp_how_to_use_raw",
    "pdp_faq_items",
    "pdp_ingredients_raw",
    "pdp_review_summary",
    "ingredient_intel",
    "key_ingredients",
    "inci_list",
    "ingredient_tokens",
    "commerce_facts_v1",
    "pdp_force_fill_v1",
    "pdp_field_capture_status",
    "pdp_field_quality_summary",
    "bundle_components",
    "bundle_component_refs",
    "review_summary",
    "shade_detail_label",
    "variant_detail_label",
    "product_kind",
)


SELECT_SQL = """
SELECT product_key, brand, product_payload
FROM catalog_products
WHERE merchant_id = 'external_seed'
  AND product_payload IS NOT NULL
ORDER BY product_key
{limit_clause}
"""


UPDATE_SQL = """
UPDATE catalog_products
SET product_payload = CAST(:product_payload AS jsonb),
    updated_at = now()
WHERE product_key = :product_key
"""


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="UPDATE rows. Default is dry-run.")
    parser.add_argument("--limit", type=int, default=0, help="Limit rows touched (0 = all).")
    parser.add_argument("--sample-limit", type=int, default=5, help="Sample rows in report.")
    parser.add_argument("--output-json", default=None)
    parser.add_argument("--output-md", default=None)
    return parser.parse_args()


def _write_if_requested(path_str: Optional[str], content: str) -> None:
    if not path_str:
        return
    path = Path(path_str)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _coerce_payload(value: Any) -> Optional[Dict[str, Any]]:
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return None
    return None


def _build_updated_payload(brand_column: Any, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Lift force-fill keys from payload['seed_data'] to top-level and
    set a structured brand object. Returns the new payload or None if
    nothing changes (skip update)."""
    seed_data = _coerce_payload(payload.get("seed_data")) or {}
    new_payload = dict(payload)

    changed = False

    # Add brand object if missing or stale
    brand_display = proper_case_brand(brand_column) if brand_column else ""
    if brand_display:
        existing_brand = payload.get("brand")
        wanted = {"name": brand_display}
        if existing_brand != wanted:
            new_payload["brand"] = wanted
            changed = True

    # Lift rich keys from seed_data to top-level if absent
    for key in RICH_KEYS:
        if key in seed_data and seed_data[key] is not None:
            if key not in new_payload or new_payload[key] is None:
                new_payload[key] = seed_data[key]
                changed = True

    return new_payload if changed else None


async def _fetch_candidates(limit: int) -> List[Dict[str, Any]]:
    limit_clause = f"LIMIT {int(limit)}" if limit and limit > 0 else ""
    sql = SELECT_SQL.format(limit_clause=limit_clause)
    rows = await database.fetch_all(sql)
    return [dict(r) for r in (rows or [])]


def _plan_updates(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    plan: List[Dict[str, Any]] = []
    for row in rows:
        payload = _coerce_payload(row.get("product_payload"))
        if not isinstance(payload, dict):
            continue
        new_payload = _build_updated_payload(row.get("brand"), payload)
        if new_payload is None:
            continue
        # Track which keys we're adding for the report
        added_keys = [k for k in (("brand",) + RICH_KEYS) if k in new_payload and k not in payload]
        plan.append(
            {
                "product_key": row["product_key"],
                "added_keys": added_keys,
                "new_payload": new_payload,
            }
        )
    return plan


async def _apply_updates(plan: List[Dict[str, Any]]) -> int:
    updated = 0
    for item in plan:
        await database.execute(
            UPDATE_SQL,
            {
                "product_payload": json.dumps(item["new_payload"], ensure_ascii=False),
                "product_key": item["product_key"],
            },
        )
        updated += 1
    return updated


def _summarize(plan: List[Dict[str, Any]]) -> Dict[str, Any]:
    by_key: Dict[str, int] = {}
    for item in plan:
        for k in item["added_keys"]:
            by_key[k] = by_key.get(k, 0) + 1
    return {
        "candidates": len(plan),
        "keys_added_distribution": sorted(by_key.items(), key=lambda kv: -kv[1]),
    }


def _render_markdown(report: Dict[str, Any]) -> str:
    lines = [
        "# External-Seed product_payload backfill",
        "",
        f"- generated_at: `{report['generated_at']}`",
        f"- apply: `{report['apply']}`",
        f"- candidates: `{report['summary']['candidates']}`",
        f"- updates_applied: `{report.get('updates_applied', 0)}`",
        "",
        "## Keys added per row (distribution)",
        "",
    ]
    for key, n in report["summary"]["keys_added_distribution"]:
        lines.append(f"- `{key}`: {n}")
    lines.append("")
    lines.append("## Sample product_keys")
    for s in report.get("sample", []):
        lines.append(f"- `{s['product_key']}` (added: {', '.join(s['added_keys'])})")
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
            "scanned": len(rows),
            "summary": _summarize(plan),
            "sample": [
                {"product_key": p["product_key"], "added_keys": p["added_keys"]}
                for p in plan[: args.sample_limit]
            ],
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
