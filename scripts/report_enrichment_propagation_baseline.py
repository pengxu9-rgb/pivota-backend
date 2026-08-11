"""Read-only baseline for the enrichment-propagation workstream.

Answers one question with numbers instead of adjectives: how much Pivota
enrichment exists, and how much of it actually reaches the served
`agent_pdp_view`. Run it before a backfill to size the job, and after to prove
the job moved something.

MEASURED AT THE SOURCE TABLES, NOT THE SERVING VIEW. The Phase 2 handoff records
this being got wrong twice: `bullet_points` read 0/39 sampled at the view and was
written off as "the data doesn't exist", when `product_enrichment` held 360 rows,
355 of them with bullet_points. A view-level sample bounds prevalence; it does
not measure the source. Every count here starts at product_enrichment or
catalog_products.

THE JOIN IS THE FRAGILE PART. product_enrichment names the third leg of the
catalog identity triple `platform_product_id`; on catalog_products the same value
is `source_product_id`. Getting that wrong does not raise — it returns zero rows,
which reads exactly like "there is nothing to backfill". That mistake shipped
once already, in refresh_agent_pdp_view_for_enrichment_write, where it silently
disabled the on-write publish bridge entirely. `identity_join_sanity` below
exists to make that failure loud: if `rows_that_join` is 0 while
`enrichment_rows` is not, the join is wrong, not the corpus empty.

Read-only: every statement is a SELECT. There is no --apply and nothing to undo.

Usage
-----
  python3 scripts/report_enrichment_propagation_baseline.py
  python3 scripts/report_enrichment_propagation_baseline.py --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any, Dict, List, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.database import database  # noqa: E402

# The identity join, written once and reused, so the two halves of this report
# cannot drift apart. Mirrors scripts/backfill_agent_pdp_view.py's cohort query.
_IDENTITY_JOIN = """
    FROM product_enrichment pe
    JOIN catalog_products cp
      ON cp.merchant_id = pe.merchant_id
     AND cp.platform = pe.platform
     AND cp.source_product_id = pe.platform_product_id
    LEFT JOIN agent_pdp_view av ON av.content_key = cp.content_key
    WHERE cp.content_key IS NOT NULL
"""

# "Carries real content" — an enrichment row can exist with every overlay column
# NULL (the pipeline writes a row per product it considers). Only rows with
# something to publish are worth counting as stranded.
_HAS_CONTENT = """
      AND (pe.bullet_points IS NOT NULL
           OR pe.usage_scenarios IS NOT NULL
           OR (pe.description_markdown IS NOT NULL
               AND btrim(pe.description_markdown) <> ''))
"""

_NOT_YET_SERVING = """
      AND av.bullet_points IS NULL
      AND av.usage_scenarios IS NULL
"""

# Labels are dict keys in the JSON output, so they must be UNIQUE — the source
# and the view both have a `bullet_points` count, and sharing a label silently
# overwrote one with the other. A report that drops half its rows without
# erroring is precisely the kind of quiet wrong number this file exists to avoid.
SOURCE_COUNTS: List[Tuple[str, str]] = [
    ("product_enrichment: rows (all geo)",
     "SELECT COUNT(*) FROM product_enrichment"),
    ("product_enrichment: geo_code = 'default'",
     "SELECT COUNT(*) FROM product_enrichment WHERE geo_code = 'default'"),
    ("product_enrichment: with bullet_points",
     "SELECT COUNT(*) FROM product_enrichment WHERE bullet_points IS NOT NULL"),
    ("product_enrichment: with usage_scenarios",
     "SELECT COUNT(*) FROM product_enrichment WHERE usage_scenarios IS NOT NULL"),
    ("product_enrichment: with description_markdown",
     "SELECT COUNT(*) FROM product_enrichment "
     "WHERE description_markdown IS NOT NULL AND btrim(description_markdown) <> ''"),
    ("agent_pdp_view: rows (total)",
     "SELECT COUNT(*) FROM agent_pdp_view"),
    ("agent_pdp_view: with bullet_points",
     "SELECT COUNT(*) FROM agent_pdp_view WHERE bullet_points IS NOT NULL"),
    ("agent_pdp_view: with usage_scenarios",
     "SELECT COUNT(*) FROM agent_pdp_view WHERE usage_scenarios IS NOT NULL"),
]

IDENTITY_JOIN_SANITY_SQL = """
    SELECT
      (SELECT COUNT(*) FROM product_enrichment) AS enrichment_rows,
      (SELECT COUNT(*) FROM product_enrichment pe
         WHERE EXISTS (
           SELECT 1 FROM catalog_products cp
           WHERE cp.merchant_id = pe.merchant_id
             AND cp.platform = pe.platform
             AND cp.source_product_id = pe.platform_product_id)
      ) AS rows_that_join
"""

COHORT_SQL = f"""
    SELECT
      COUNT(DISTINCT cp.content_key) AS enriched_content_keys,
      COUNT(DISTINCT cp.content_key)
        FILTER (WHERE av.content_key IS NOT NULL) AS have_a_view_row,
      COUNT(DISTINCT cp.content_key)
        FILTER (WHERE av.bullet_points IS NOT NULL
                   OR av.usage_scenarios IS NOT NULL) AS already_propagated
    {_IDENTITY_JOIN}
"""

STRANDED_SQL = f"""
    SELECT COUNT(DISTINCT cp.content_key) AS stranded_with_content
    {_IDENTITY_JOIN}
    {_HAS_CONTENT}
    {_NOT_YET_SERVING}
"""

STRANDED_BY_BRAND_SQL = f"""
    SELECT COALESCE(cp.brand, '(no brand)') AS brand,
           COUNT(DISTINCT cp.content_key) AS content_keys
    {_IDENTITY_JOIN}
    {_HAS_CONTENT}
    {_NOT_YET_SERVING}
    GROUP BY 1
    ORDER BY content_keys DESC, brand ASC
    LIMIT 25
"""


async def collect() -> Dict[str, Any]:
    if not getattr(database, "is_connected", False):
        await database.connect()

    out: Dict[str, Any] = {"source_counts": {}}
    for label, sql in SOURCE_COUNTS:
        key = label.strip()
        if key in out["source_counts"]:
            raise AssertionError(
                f"duplicate SOURCE_COUNTS label {key!r} — it would overwrite the "
                "earlier count and drop a row from the report silently"
            )
        out["source_counts"][key] = await database.fetch_val(sql)

    sanity = dict(await database.fetch_one(IDENTITY_JOIN_SANITY_SQL) or {})
    out["identity_join_sanity"] = sanity
    out["cohort"] = dict(await database.fetch_one(COHORT_SQL) or {})
    out["stranded"] = dict(await database.fetch_one(STRANDED_SQL) or {})
    out["stranded_by_brand"] = [
        dict(r) for r in (await database.fetch_all(STRANDED_BY_BRAND_SQL) or [])
    ]

    # Loud, not buried in the numbers: a zero join with a non-zero source is the
    # signature of the identity-spelling bug, and is NOT "nothing to backfill".
    if sanity.get("enrichment_rows") and not sanity.get("rows_that_join"):
        out["WARNING"] = (
            "product_enrichment has rows but NONE join catalog_products on "
            "source_product_id. The identity join is wrong — do not read this as "
            "an empty backfill."
        )
    return out


def render(report: Dict[str, Any]) -> str:
    lines: List[str] = ["=== source-table counts ==="]
    for label, value in report["source_counts"].items():
        lines.append(f"  {label:<44} {value}")
    for title, key in (
        ("identity-join sanity", "identity_join_sanity"),
        ("enriched cohort", "cohort"),
        ("stranded (has content, not serving)", "stranded"),
    ):
        lines.append(f"\n=== {title} ===")
        for k, v in (report.get(key) or {}).items():
            lines.append(f"  {k:<44} {v}")
    lines.append("\n=== stranded by brand (top 25) ===")
    rows = report.get("stranded_by_brand") or []
    if not rows:
        lines.append("  (none)")
    for r in rows:
        lines.append(f"  {str(r['brand'])[:43]:<44} {r['content_keys']}")
    if report.get("WARNING"):
        lines.append("\n!! " + report["WARNING"])
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
