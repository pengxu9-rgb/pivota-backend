"""Is the INCI that landed actually INCI? Quality audit of beauty_sku_ingredients.

The 2026-08-13 depth scorecard found INCI ingested at 5,195 products against the
handoff's ~135 baseline — a 38x jump that exceeds the 3,122 valid captured seeds,
so non-seed ingest paths ran, and none of it came from this workstream. Before
anyone builds on it (ingredient_concern grounding, INCI-substantiated claims —
the "K-beauty wedge" tier the AEO portfolio says we are most likely to win),
verify the material is real:

  * VALIDITY, post-hoc: backfill_seed_inci gates ingestion at >= 20 chars and
    >= 4 commas ("marketing 'key ingredients' bullets are not INCI") — but other
    ingest paths may not. Count what is sitting in the table that would FAIL the
    gate that was supposed to protect it.
  * CONTAMINATION: HTML fragments, "key ingredients" marketing prose.
  * BOILERPLATE: one raw_inci string shared across many products is a template,
    not a formulation. Top shared strings, with counts.
  * PROVENANCE + FRESHNESS: source_system distribution and the write window —
    attributes the 135 -> 5,195 jump to a when and a what.
  * REACH: how much of the INCI-carrying corpus is actually serving-eligible,
    i.e. can the ingredient depth reach an agent at all.

Read-only: every statement is a SELECT; there is no --apply.

Usage
-----
  python3 scripts/report_inci_ingestion_quality.py
  python3 scripts/report_inci_ingestion_quality.py --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.database import database  # noqa: E402

# The ingest gate, verbatim from scripts/backfill_seed_inci.py
# (MIN_INCI_LEN = 20, MIN_INCI_COMMAS = 4), spelled once as SQL.
_VALID = (
    "LENGTH(raw_inci) >= 20 "
    "AND LENGTH(raw_inci) - LENGTH(REPLACE(raw_inci, ',', '')) >= 4"
)

TOTALS_SQL = f"""
    SELECT
      COUNT(*) AS rows_total,
      COUNT(DISTINCT product_key) AS products_total,
      COUNT(*) FILTER (WHERE COALESCE(raw_inci, '') <> '') AS rows_with_inci,
      COUNT(DISTINCT product_key) FILTER (
        WHERE COALESCE(raw_inci, '') <> '') AS products_with_inci,
      COUNT(*) FILTER (WHERE COALESCE(raw_inci, '') <> ''
                         AND NOT ({_VALID})) AS fails_ingest_gate,
      COUNT(*) FILTER (WHERE raw_inci LIKE '%<%') AS html_contaminated,
      COUNT(*) FILTER (WHERE raw_inci ILIKE '%key ingredient%') AS marketing_prose,
      MIN(LENGTH(raw_inci)) FILTER (WHERE COALESCE(raw_inci, '') <> '') AS len_min,
      ROUND(AVG(LENGTH(raw_inci)) FILTER (
        WHERE COALESCE(raw_inci, '') <> '')) AS len_avg,
      MAX(LENGTH(raw_inci)) FILTER (WHERE COALESCE(raw_inci, '') <> '') AS len_max
    FROM beauty_sku_ingredients
"""

GATE_FAIL_SAMPLES_SQL = f"""
    SELECT product_key, LEFT(raw_inci, 70) AS raw_inci_head, LENGTH(raw_inci) AS len
    FROM beauty_sku_ingredients
    WHERE COALESCE(raw_inci, '') <> '' AND NOT ({_VALID})
    ORDER BY product_key
    LIMIT 8
"""

BOILERPLATE_SQL = """
    SELECT LEFT(raw_inci, 60) AS raw_inci_head,
           COUNT(DISTINCT product_key) AS products
    FROM beauty_sku_ingredients
    WHERE COALESCE(raw_inci, '') <> ''
    GROUP BY raw_inci
    HAVING COUNT(DISTINCT product_key) >= 5
    ORDER BY products DESC
    LIMIT 8
"""

PROVENANCE_SQL = """
    SELECT COALESCE(source_system, '(null)') AS source_system,
           COUNT(DISTINCT product_key) AS products,
           MIN(updated_at) AS oldest,
           MAX(updated_at) AS newest
    FROM beauty_sku_ingredients
    WHERE COALESCE(raw_inci, '') <> ''
    GROUP BY 1
    ORDER BY products DESC
"""

FRESHNESS_SQL = """
    SELECT to_char(date_trunc('day', updated_at), 'YYYY-MM-DD') AS day,
           COUNT(DISTINCT product_key) AS products
    FROM beauty_sku_ingredients
    WHERE COALESCE(raw_inci, '') <> ''
    GROUP BY 1
    ORDER BY 1 DESC
    LIMIT 10
"""

REACH_SQL = """
    SELECT
      COUNT(DISTINCT bsi.product_key) AS inci_products,
      COUNT(DISTINCT bsi.product_key) FILTER (
        WHERE ips.serving_eligible IS TRUE) AS serving_eligible,
      COUNT(DISTINCT bsi.product_key) FILTER (
        WHERE av.evidence_profile IS NOT NULL) AS evidence_on_served_row
    FROM beauty_sku_ingredients bsi
    JOIN catalog_products cp ON cp.product_key = bsi.product_key
    LEFT JOIN index_pipeline_state ips ON ips.content_key = cp.content_key
    LEFT JOIN agent_pdp_view av ON av.content_key = cp.content_key
    WHERE COALESCE(bsi.raw_inci, '') <> ''
"""


async def collect() -> Dict[str, Any]:
    if not getattr(database, "is_connected", False):
        await database.connect()

    out: Dict[str, Any] = {}
    out["totals"] = dict(await database.fetch_one(TOTALS_SQL) or {})
    out["gate_fail_samples"] = [
        dict(r) for r in (await database.fetch_all(GATE_FAIL_SAMPLES_SQL) or [])
    ]
    out["boilerplate"] = [
        dict(r) for r in (await database.fetch_all(BOILERPLATE_SQL) or [])
    ]
    out["provenance"] = [
        dict(r) for r in (await database.fetch_all(PROVENANCE_SQL) or [])
    ]
    out["by_day"] = [dict(r) for r in (await database.fetch_all(FRESHNESS_SQL) or [])]
    out["reach"] = dict(await database.fetch_one(REACH_SQL) or {})
    return out


def render(report: Dict[str, Any]) -> str:
    t = report["totals"]
    lines: List[str] = ["=== 1. totals and validity (gate: >=20 chars, >=4 commas) ==="]
    for k in ("rows_total", "products_total", "rows_with_inci", "products_with_inci",
              "fails_ingest_gate", "html_contaminated", "marketing_prose",
              "len_min", "len_avg", "len_max"):
        lines.append(f"  {k:<22} {t.get(k)}")

    lines.append("\n=== 2. rows that FAIL the ingest gate (should be zero) ===")
    if not report["gate_fail_samples"]:
        lines.append("  (none)")
    for r in report["gate_fail_samples"]:
        lines.append(f"  len={r['len']:>4}  {r['raw_inci_head']}")

    lines.append("\n=== 3. boilerplate: identical raw_inci across >=5 products ===")
    if not report["boilerplate"]:
        lines.append("  (none)")
    for r in report["boilerplate"]:
        lines.append(f"  x{r['products']:<5} {r['raw_inci_head']}")

    lines.append("\n=== 4. provenance ===")
    for r in report["provenance"]:
        lines.append(f"  {str(r['source_system'])[:28]:<30}{r['products']:>7}   "
                     f"{str(r['oldest'])[:10]} .. {str(r['newest'])[:10]}")

    lines.append("\n=== 5. products by last-write day (top 10) ===")
    for r in report["by_day"]:
        lines.append(f"  {r['day']}  {r['products']}")

    lines.append("\n=== 6. reach: does the INCI corpus serve? ===")
    for k, v in report["reach"].items():
        lines.append(f"  {k:<26} {v}")
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--json", action="store_true", help="Emit raw JSON.")
    args = p.parse_args()
    report = asyncio.run(collect())
    print(json.dumps(report, indent=2, default=str) if args.json else render(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
