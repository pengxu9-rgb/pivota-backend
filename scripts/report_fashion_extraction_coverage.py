#!/usr/bin/env python3
"""Per-merchant fashion-field extraction coverage report (read-only).

Answers: for each merchant, how many fashion-categorized catalog_products
rows have material / care / size_guide populated, grouped by which
extractor produced the value, and how many remain NULL — split into
"has source text" (enrichable) vs "no source text" (can never be
auto-enriched).

Inputs: catalog_products (and joined external_product_seeds for the
seed_data fallback haystack — same columns the backfill script reads).

Outputs: JSON to stdout. Pipe through `jq` or save for review.

No writes. No LLM calls. Safe to run against prod.

Usage:
  python scripts/report_fashion_extraction_coverage.py
  python scripts/report_fashion_extraction_coverage.py --merchant-id merch_pawstyle
  python scripts/report_fashion_extraction_coverage.py --format md
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


# Mirrors services/fashion_field_extractor._FASHION_CATEGORY_PREFIXES.
# Keep in sync — if a new prefix is added there, add it here.
_FASHION_PREFIXES = ("fashion/", "apparel/", "clothing/", "shoes/", "accessories/")
_FIELDS = ("material", "care", "size_guide")


def _fashion_where_clause(alias: str = "cp") -> str:
    """SQL fragment matching the same fashion gate the extractor uses."""
    parts = [
        f"LOWER({alias}.category_path) LIKE '{p}%'" for p in _FASHION_PREFIXES
    ]
    return "(" + " OR ".join(parts) + ")"


async def _per_merchant_rollup(
    *, merchant_id: Optional[str]
) -> List[Dict[str, Any]]:
    """One row per merchant with totals + fashion-row counts."""
    where_merchant = (
        "AND cp.merchant_id = :merchant_id" if merchant_id else ""
    )
    sql = f"""
        SELECT
          cp.merchant_id,
          cp.platform,
          COUNT(*) AS total_rows,
          COUNT(*) FILTER (WHERE {_fashion_where_clause('cp')}) AS fashion_rows
        FROM catalog_products cp
        WHERE 1=1 {where_merchant}
        GROUP BY cp.merchant_id, cp.platform
        ORDER BY fashion_rows DESC, total_rows DESC
    """
    rows = await database.fetch_all(
        sql, {"merchant_id": merchant_id} if merchant_id else {}
    )
    return [dict(r) for r in rows or []]


async def _per_field_breakdown(
    *, merchant_id: Optional[str], field: str
) -> Dict[str, Any]:
    """For one field (material/care/size_guide), per-merchant:
      - populated counts grouped by *_source value
      - NULL counts split by has_source_text vs no_source_text
    Only counts rows passing the fashion category gate.
    """
    where_merchant = (
        "AND cp.merchant_id = :merchant_id" if merchant_id else ""
    )
    source_col = f"{field}_source"
    value_col = field

    populated_sql = f"""
        SELECT
          cp.merchant_id,
          cp.platform,
          COALESCE(cp.{source_col}, 'unknown') AS source,
          COUNT(*) AS n
        FROM catalog_products cp
        WHERE cp.{value_col} IS NOT NULL
          AND {_fashion_where_clause('cp')}
          {where_merchant}
        GROUP BY cp.merchant_id, cp.platform, COALESCE(cp.{source_col}, 'unknown')
        ORDER BY cp.merchant_id, cp.platform, n DESC
    """

    # "has_source_text" mirrors what backfill_fashion_fields._description_haystack
    # would assemble: description column, OR product_payload has description /
    # description_text / body_html, OR external_product_seeds.seed_data.snapshot
    # has equivalent keys. Cheap server-side approximation: just check the
    # most common signal — non-empty description OR non-empty product_payload.
    null_sql = f"""
        SELECT
          cp.merchant_id,
          cp.platform,
          (
            COALESCE(NULLIF(TRIM(cp.description), ''), '') <> ''
            OR (cp.product_payload IS NOT NULL
                AND cp.product_payload::text NOT IN ('null', '{{}}', '[]'))
          ) AS has_source_text,
          COUNT(*) AS n
        FROM catalog_products cp
        WHERE cp.{value_col} IS NULL
          AND {_fashion_where_clause('cp')}
          {where_merchant}
        GROUP BY cp.merchant_id, cp.platform, has_source_text
        ORDER BY cp.merchant_id, cp.platform
    """

    params = {"merchant_id": merchant_id} if merchant_id else {}
    populated = [dict(r) for r in await database.fetch_all(populated_sql, params) or []]
    nulls = [dict(r) for r in await database.fetch_all(null_sql, params) or []]

    # Reshape into per-(merchant, platform) dict
    per_merchant: Dict[tuple, Dict[str, Any]] = {}
    for r in populated:
        key = (r["merchant_id"], r["platform"])
        per_merchant.setdefault(key, {"populated_by_source": {}, "null_breakdown": {}})
        per_merchant[key]["populated_by_source"][r["source"]] = r["n"]
    for r in nulls:
        key = (r["merchant_id"], r["platform"])
        per_merchant.setdefault(key, {"populated_by_source": {}, "null_breakdown": {}})
        bkey = "has_source_text" if r["has_source_text"] else "no_source_text"
        per_merchant[key]["null_breakdown"][bkey] = r["n"]

    return per_merchant


async def build_report(*, merchant_id: Optional[str]) -> Dict[str, Any]:
    rollup = await _per_merchant_rollup(merchant_id=merchant_id)
    field_reports = {}
    for field in _FIELDS:
        field_reports[field] = await _per_field_breakdown(
            merchant_id=merchant_id, field=field
        )

    # Merge: for each (merchant, platform) row in the rollup, attach field breakdowns
    merchants_out: List[Dict[str, Any]] = []
    for r in rollup:
        mid = r["merchant_id"]
        plat = r["platform"]
        merchant_block: Dict[str, Any] = {
            "merchant_id": mid,
            "platform": plat,
            "total_rows": r["total_rows"],
            "fashion_rows": r["fashion_rows"],
            "fields": {},
        }
        for field in _FIELDS:
            block = field_reports[field].get((mid, plat), {})
            populated = block.get("populated_by_source", {})
            null = block.get("null_breakdown", {})
            populated_total = sum(populated.values())
            null_total = sum(null.values())
            merchant_block["fields"][field] = {
                "populated": populated_total,
                "populated_by_source": populated,
                "null": null_total,
                "null_has_source_text": null.get("has_source_text", 0),
                "null_no_source_text": null.get("no_source_text", 0),
                "coverage_pct": (
                    round(100.0 * populated_total / r["fashion_rows"], 1)
                    if r["fashion_rows"] else 0.0
                ),
                "enrichable_pct": (
                    round(
                        100.0
                        * (populated_total + null.get("has_source_text", 0))
                        / r["fashion_rows"],
                        1,
                    )
                    if r["fashion_rows"]
                    else 0.0
                ),
            }
        merchants_out.append(merchant_block)

    # Global totals
    totals: Dict[str, Any] = {"fashion_rows": 0, "fields": {}}
    for field in _FIELDS:
        totals["fields"][field] = {
            "populated": 0,
            "populated_by_source": {},
            "null_has_source_text": 0,
            "null_no_source_text": 0,
        }
    for m in merchants_out:
        totals["fashion_rows"] += m["fashion_rows"]
        for field in _FIELDS:
            fb = m["fields"][field]
            tb = totals["fields"][field]
            tb["populated"] += fb["populated"]
            tb["null_has_source_text"] += fb["null_has_source_text"]
            tb["null_no_source_text"] += fb["null_no_source_text"]
            for src, n in fb["populated_by_source"].items():
                tb["populated_by_source"][src] = tb["populated_by_source"].get(src, 0) + n
    for field in _FIELDS:
        tb = totals["fields"][field]
        denom = totals["fashion_rows"] or 1
        tb["coverage_pct"] = round(100.0 * tb["populated"] / denom, 1)
        tb["enrichable_pct"] = round(
            100.0 * (tb["populated"] + tb["null_has_source_text"]) / denom, 1
        )

    return {
        "scope": {"merchant_id": merchant_id} if merchant_id else {"merchant_id": None},
        "merchants": merchants_out,
        "totals": totals,
    }


async def _canonical_view_inheritance(
    *, merchant_id: Optional[str]
) -> Dict[str, Any]:
    """Count agent_pdp_view rows where a fashion field is populated
    by inheritance from a co-merchant or external_seed, versus rows
    where the field is populated only because the merchant's own row
    has it. The lift between (merchant-row populated) and (canonical
    view populated) is the value the cross-PDP coalesce adds.

    Scopes to content_keys touched by the merchant when --merchant-id
    is set; otherwise reports across all fashion content_keys.
    """
    where_merchant_join = (
        "INNER JOIN catalog_products cp_scope "
        "  ON cp_scope.content_key = apv.content_key "
        "  AND cp_scope.merchant_id = :merchant_id"
        if merchant_id else ""
    )
    sql = f"""
        SELECT
          COUNT(*) FILTER (
            WHERE (
              LOWER(apv.category_path) LIKE 'fashion/%'
              OR LOWER(apv.category_path) LIKE 'apparel/%'
              OR LOWER(apv.category_path) LIKE 'clothing/%'
              OR LOWER(apv.category_path) LIKE 'shoes/%'
              OR LOWER(apv.category_path) LIKE 'accessories/%'
            )
          ) AS fashion_canonicals,
          COUNT(*) FILTER (WHERE apv.material IS NOT NULL) AS material_populated,
          COUNT(*) FILTER (WHERE apv.care IS NOT NULL) AS care_populated,
          COUNT(*) FILTER (WHERE apv.size_guide IS NOT NULL) AS size_guide_populated,
          COUNT(*) FILTER (
            WHERE apv.material_source = 'merchant_payload'
          ) AS material_from_merchant_payload,
          COUNT(*) FILTER (
            WHERE apv.material_source = 'merchant_authored'
          ) AS material_from_merchant_authored,
          COUNT(*) FILTER (
            WHERE apv.material_source = 'llm_extraction_v1'
          ) AS material_from_llm,
          COUNT(*) FILTER (
            WHERE apv.material_source = 'external_seed'
          ) AS material_from_external_seed
        FROM agent_pdp_view apv
        {where_merchant_join}
    """
    params = {"merchant_id": merchant_id} if merchant_id else {}
    row = await database.fetch_one(sql, params)
    return dict(row) if row else {}


def _format_markdown(report: Dict[str, Any]) -> str:
    lines = ["# Fashion-field extraction coverage", ""]
    t = report["totals"]
    lines.append(f"**Total fashion rows:** {t['fashion_rows']}")
    lines.append("")
    lines.append("| Field | Populated | Coverage | NULL+source-text | NULL+no-source | Enrichable ceiling |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    for field in _FIELDS:
        tb = t["fields"][field]
        lines.append(
            f"| {field} | {tb['populated']} | {tb['coverage_pct']}% | "
            f"{tb['null_has_source_text']} | {tb['null_no_source_text']} | "
            f"{tb['enrichable_pct']}% |"
        )
    lines.append("")
    lines.append("## Populated rows by extractor source (totals)")
    lines.append("")
    for field in _FIELDS:
        sources = t["fields"][field]["populated_by_source"]
        if not sources:
            lines.append(f"- **{field}**: (none populated)")
            continue
        parts = ", ".join(f"{s}={n}" for s, n in sorted(sources.items()))
        lines.append(f"- **{field}**: {parts}")
    lines.append("")
    lines.append("## Per merchant")
    lines.append("")
    for m in report["merchants"]:
        if m["fashion_rows"] == 0:
            continue
        lines.append(
            f"### `{m['merchant_id']}` ({m['platform']}) — "
            f"{m['fashion_rows']} fashion / {m['total_rows']} total"
        )
        lines.append("")
        lines.append("| Field | Pop | Cov | NULL+text | NULL+no-text | Sources |")
        lines.append("|---|---:|---:|---:|---:|---|")
        for field in _FIELDS:
            fb = m["fields"][field]
            sources = ", ".join(
                f"{s}:{n}" for s, n in sorted(fb["populated_by_source"].items())
            ) or "—"
            lines.append(
                f"| {field} | {fb['populated']} | {fb['coverage_pct']}% | "
                f"{fb['null_has_source_text']} | {fb['null_no_source_text']} | "
                f"{sources} |"
            )
        lines.append("")
    return "\n".join(lines)


async def _run(args: argparse.Namespace) -> int:
    if not getattr(database, "is_connected", False):
        await database.connect()
    try:
        report = await build_report(merchant_id=args.merchant_id)
        if args.include_canonical_view:
            report["canonical_view"] = await _canonical_view_inheritance(
                merchant_id=args.merchant_id
            )
        if args.format == "md":
            print(_format_markdown(report))
            if args.include_canonical_view:
                cv = report["canonical_view"]
                print("")
                print("## Canonical agent_pdp_view (cross-PDP coalesce)")
                print("")
                print(f"- Fashion canonical PDPs: {cv.get('fashion_canonicals', 0)}")
                print(f"- material populated: {cv.get('material_populated', 0)}")
                print(f"- care populated: {cv.get('care_populated', 0)}")
                print(f"- size_guide populated: {cv.get('size_guide_populated', 0)}")
                print("- material winning source counts:")
                print(f"  - merchant_payload: {cv.get('material_from_merchant_payload', 0)}")
                print(f"  - merchant_authored: {cv.get('material_from_merchant_authored', 0)}")
                print(f"  - llm_extraction_v1: {cv.get('material_from_llm', 0)}")
                print(f"  - external_seed: {cv.get('material_from_external_seed', 0)}")
        else:
            print(json.dumps(report, indent=2, sort_keys=True, default=str))
        return 0
    finally:
        if getattr(database, "is_connected", False):
            await database.disconnect()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--merchant-id",
        default=None,
        help="Restrict report to a single merchant_id. Default: all merchants.",
    )
    parser.add_argument(
        "--format",
        choices=("json", "md"),
        default="json",
        help="Output format (default: json).",
    )
    parser.add_argument(
        "--include-canonical-view",
        action="store_true",
        help="Additionally query agent_pdp_view and report cross-PDP "
             "coalesced material/care/size_guide coverage (the user-facing "
             "post-inheritance numbers). Off by default — keeps single-merchant "
             "audits fast.",
    )
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
