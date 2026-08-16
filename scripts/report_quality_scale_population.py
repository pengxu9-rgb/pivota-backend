"""How many products are gated on a quality score from a SUPERSEDED scorer?

The 2026-08-11 investigation found three products (HBN Double Retinol, two Anuko
rows) blocked as `low_quality` while carrying `v1-lite` scores — the ORIGINAL
scorer, two component-set changes behind the `v3-six-components` scale the 71.4
gate compares against. Those three surfaced only because they happened to
intersect the enriched cohort. This measures the whole population.

WHY "LATEST PER PRODUCT" IS THE ONLY HONEST GRAIN. product_quality_snapshot is
APPEND-ONLY (services/product_quality_service.py: a bare insert(), no
ON CONFLICT), so one product accumulates a row per scoring run and a raw
GROUP BY rules_version counts history, not exposure. The serving classifier reads
exactly one row per product — `_ELIGIBILITY_LATERAL_JOINS` does
`ORDER BY snapshot_date DESC LIMIT 1` per (merchant, platform, platform_product_id)
— so the DISTINCT ON below mirrors that predicate exactly. A product with an old
v1-lite row AND a newer v3 row is CURRENT, and must not be counted as stale.

WHAT THIS DELIBERATELY DOES NOT DO: convert an old-scale score to the new scale.
v1 -> v3 changed the component SET, not merely the divisor, and
product_quality_service says v1 snapshots "are not comparable and must be
re-scored". The 7/6 arithmetic that relates v2 to v3 does not apply. Scores are
therefore reported in BANDS around the live threshold so the size of the
near-miss population is visible, without asserting what any row would score.

Read-only: every statement is a SELECT; there is no --apply.

Usage
-----
  python3 scripts/report_quality_scale_population.py
  python3 scripts/report_quality_scale_population.py --json
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

# Mirrors _ELIGIBILITY_LATERAL_JOINS' predicate: newest snapshot per identity.
_LATEST_SQL = """
    SELECT DISTINCT ON (merchant_id, platform, platform_product_id)
           merchant_id, platform, platform_product_id,
           rules_version, content_quality_score, snapshot_date
    FROM product_quality_snapshot
    ORDER BY merchant_id, platform, platform_product_id, snapshot_date DESC
"""

# 1. The whole corpus, by the scorer version its CURRENT score came from.
BY_VERSION_SQL = f"""
    WITH latest AS ({_LATEST_SQL})
    SELECT
      COALESCE(rules_version, '(null)') AS rules_version,
      COUNT(*) AS products,
      ROUND(MIN(content_quality_score)::numeric, 1) AS min_score,
      ROUND(AVG(content_quality_score)::numeric, 1) AS avg_score,
      ROUND(MAX(content_quality_score)::numeric, 1) AS max_score,
      MIN(snapshot_date)::date AS oldest,
      MAX(snapshot_date)::date AS newest
    FROM latest
    GROUP BY 1
    ORDER BY products DESC
"""

# 2. The subset that COSTS something: stale-scored AND currently blocked as
#    low_quality. A stale score on an already-serving row is harmless.
STALE_AND_BLOCKED_SQL = f"""
    WITH latest AS ({_LATEST_SQL})
    SELECT
      COALESCE(l.rules_version, '(null)') AS rules_version,
      cp.platform,
      COUNT(*) AS blocked_low_quality,
      ROUND(MIN(ips.content_quality_score)::numeric, 1) AS min_score,
      ROUND(MAX(ips.content_quality_score)::numeric, 1) AS max_score
    FROM latest l
    JOIN catalog_products cp
      ON cp.merchant_id = l.merchant_id
     AND cp.platform = l.platform
     AND cp.source_product_id = l.platform_product_id
    JOIN index_pipeline_state ips ON ips.content_key = cp.content_key
    WHERE ips.serving_eligible IS DISTINCT FROM TRUE
      -- 'not_scored' rides with 'low_quality' (2026-08-15 split). This
      -- report's `no_score` band counts UNSCORED rows, so keying on
      -- 'low_quality' alone would make that band structurally 0.
      AND COALESCE(ips.blocker_code, '') IN ('low_quality', 'not_scored')
      AND COALESCE(l.rules_version, '') <> :current_rules_version
    GROUP BY 1, 2
    ORDER BY blocked_low_quality DESC, rules_version ASC
"""

# 3. Score bands for that blocked-and-stale set. Bands, not a converted score:
#    the near-71.4 population is what a rescore could plausibly move, and the
#    far-below population is what it could not, whatever the scale.
BANDS_SQL = f"""
    WITH latest AS ({_LATEST_SQL}),
    blocked AS (
      SELECT ips.content_quality_score AS score
      FROM latest l
      JOIN catalog_products cp
        ON cp.merchant_id = l.merchant_id
       AND cp.platform = l.platform
       AND cp.source_product_id = l.platform_product_id
      JOIN index_pipeline_state ips ON ips.content_key = cp.content_key
      WHERE ips.serving_eligible IS DISTINCT FROM TRUE
        -- 'not_scored' rides with 'low_quality' (2026-08-15 split). The
        -- `no_score` band below counts UNSCORED rows, so keying on
        -- 'low_quality' alone would make that band structurally 0.
        AND COALESCE(ips.blocker_code, '') IN ('low_quality', 'not_scored')
        AND COALESCE(l.rules_version, '') <> :current_rules_version
    )
    SELECT
      COUNT(*) FILTER (WHERE score IS NULL) AS no_score,
      COUNT(*) FILTER (WHERE score < 40) AS band_under_40,
      COUNT(*) FILTER (WHERE score >= 40 AND score < 55) AS band_40_55,
      COUNT(*) FILTER (WHERE score >= 55 AND score < 65) AS band_55_65,
      COUNT(*) FILTER (WHERE score >= 65 AND score < :threshold) AS band_65_to_bar,
      COUNT(*) FILTER (WHERE score >= :threshold) AS at_or_above_bar
    FROM blocked
"""

# 4. Which platforms hold stale scores at all — sizes the blind spot the
#    external_seed-only rescore path cannot reach, separately from the rest.
BY_PLATFORM_SQL = f"""
    WITH latest AS ({_LATEST_SQL})
    SELECT
      l.platform,
      COUNT(*) AS products_on_stale_scale,
      COUNT(*) FILTER (WHERE l.platform = 'external_seed') AS reachable_by_rescore
    FROM latest l
    WHERE COALESCE(l.rules_version, '') <> :current_rules_version
    GROUP BY 1
    ORDER BY products_on_stale_scale DESC
    LIMIT 20
"""


async def collect() -> Dict[str, Any]:
    if not getattr(database, "is_connected", False):
        await database.connect()

    from services.index_pipeline_state_service import QUALITY_SCORE_THRESHOLD
    from services.product_quality_service import (
        SOURCE_BACKED_COMPONENTS_RULES_VERSION,
    )

    current = {"current_rules_version": SOURCE_BACKED_COMPONENTS_RULES_VERSION}
    banded = dict(current)
    banded["threshold"] = float(QUALITY_SCORE_THRESHOLD)

    return {
        "current_rules_version": SOURCE_BACKED_COMPONENTS_RULES_VERSION,
        "threshold": float(QUALITY_SCORE_THRESHOLD),
        "by_version": [dict(r) for r in (await database.fetch_all(BY_VERSION_SQL) or [])],
        "stale_and_blocked": [
            dict(r) for r in (await database.fetch_all(STALE_AND_BLOCKED_SQL, current) or [])
        ],
        "bands": dict(await database.fetch_one(BANDS_SQL, banded) or {}),
        "by_platform": [
            dict(r) for r in (await database.fetch_all(BY_PLATFORM_SQL, current) or [])
        ],
    }


def render(report: Dict[str, Any]) -> str:
    cur = report["current_rules_version"]
    bar = report["threshold"]
    lines = [f"current scorer = {cur} | gate threshold = {bar}", ""]

    lines.append("=== 1. every product, by the scorer its CURRENT score came from ===")
    lines.append(f"  {'rules_version':<24}{'products':>9}{'min':>7}{'avg':>7}{'max':>7}"
                 f"  {'oldest':<12}{'newest'}")
    for r in report["by_version"]:
        flag = "" if r["rules_version"] == cur else "   <- STALE"
        lines.append(
            f"  {str(r['rules_version'])[:23]:<24}{r['products']:>9}"
            f"{str(r['min_score']):>7}{str(r['avg_score']):>7}{str(r['max_score']):>7}"
            f"  {str(r['oldest']):<12}{r['newest']}{flag}"
        )

    lines.append("\n=== 2. stale-scored AND currently blocked low_quality (the cost) ===")
    rows = report["stale_and_blocked"]
    if not rows:
        lines.append("  (none — no blocked row is gated on a superseded score)")
    else:
        lines.append(f"  {'rules_version':<24}{'platform':<18}{'blocked':>8}{'min':>7}{'max':>7}")
        for r in rows:
            lines.append(
                f"  {str(r['rules_version'])[:23]:<24}{str(r['platform'])[:17]:<18}"
                f"{r['blocked_low_quality']:>8}{str(r['min_score']):>7}{str(r['max_score']):>7}"
            )
        total = sum(r["blocked_low_quality"] for r in rows)
        lines.append(f"  {'TOTAL':<42}{total:>8}")

    lines.append(f"\n=== 3. how close those blocked rows sit to the {bar} bar ===")
    for k, v in (report.get("bands") or {}).items():
        lines.append(f"  {k:<24} {v}")

    lines.append("\n=== 4. stale scores by platform (rescore reaches external_seed only) ===")
    lines.append(f"  {'platform':<24}{'stale':>8}{'reachable':>11}")
    for r in report["by_platform"]:
        lines.append(
            f"  {str(r['platform'])[:23]:<24}{r['products_on_stale_scale']:>8}"
            f"{r['reachable_by_rescore']:>11}"
        )
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
