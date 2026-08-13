"""Did product quality actually improve? The holistic scorecard, then vs now.

The 2026-08-11 handoff (docs/HANDOFF_2026-08-11_agent_depth_gap.md) defined the
depth gap with numbers and said how to verify movement: "re-run the same
source-table counts". This report is that re-run, corpus-wide rather than
sampled, with the recorded 2026-08-10/11 baselines printed alongside so the
answer is a delta column, not a memory.

WHAT "QUALITY" MEANS HERE, measured at three layers:

  1. SERVED DEPTH (agent_pdp_view) — what an agent actually receives per
     product: curated bullets/usage, evidence + disclaimers, ratings, GTIN,
     seller trust, description length. This is the layer the handoff's field
     table sampled.
  2. THE RECOMMENDATION SURFACE (index_pipeline_state) — how much of the corpus
     is eligible to be recommended at all, and what blocks the rest.
  3. INPUTS (source tables) — enrichment rows, INCI captured vs ingested,
     ratings on catalog rows: the raw material the depth workstreams (handoff
     workstreams 2-4) would convert into served depth.

HONESTY NOTE, so the report cannot be misread: the 2026-08-11..13 work was
REPAIR — it stopped live write paths destroying overlays, revived a publish
bridge that had never fired, and stopped the quality-scorer version drift. None
of that GENERATES depth. If the deltas below are ~zero, that is the expected
result, and the levers that would move them are the never-run handoff
workstreams (INCI ingestion, ratings expansion, GTIN capture) — not more repair.

Read-only: every statement is a SELECT; there is no --apply.

Usage
-----
  python3 scripts/report_agent_depth_scorecard.py
  python3 scripts/report_agent_depth_scorecard.py --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.database import database  # noqa: E402

# The recorded baselines this report compares against. Sources: the handoff's
# measured table (2026-08-10/11) and this workstream's own prod measurements.
# "sample" marks numbers that were an 18-product sample, not a corpus count —
# their delta is only indicative.
BASELINE_DATE = "2026-08-10/11"
BASELINES: Dict[str, Tuple[Optional[float], str]] = {
    "apv_rows":                (11122, "corpus"),
    "apv_bullet_points":       (217, "corpus"),
    "apv_usage_scenarios":     (129, "corpus"),
    "apv_rating_positive":     (186, "corpus (ratings pipe, 2026-08-10)"),
    "apv_gtin13":              (1, "corpus"),
    "apv_desc_ge_200_pct":     (83.0, "SAMPLE of 18 — indicative only"),
    "apv_evidence_profile":    (None, "not measured at baseline"),
    "apv_required_disclaimers": (None, "not measured at baseline"),
    "apv_seller_trust_offers": (None, "W8 shipped after baseline"),
    "inci_ingested_products":  (135, "approx, handoff workstream 2"),
    "inci_captured_seeds":     (3300, "approx, handoff workstream 2"),
    "enrichment_rows":         (360, "source table"),
    "serving_eligible":        (None, "not measured at baseline"),
}

# --- layer 1: served depth -------------------------------------------------
APV_DEPTH_SQL = """
    SELECT
      COUNT(*) AS apv_rows,
      COUNT(*) FILTER (WHERE bullet_points IS NOT NULL) AS apv_bullet_points,
      COUNT(*) FILTER (WHERE usage_scenarios IS NOT NULL) AS apv_usage_scenarios,
      COUNT(*) FILTER (WHERE evidence_profile IS NOT NULL) AS apv_evidence_profile,
      COUNT(*) FILTER (WHERE required_disclaimers IS NOT NULL) AS apv_required_disclaimers,
      COUNT(*) FILTER (WHERE rating_value IS NOT NULL
                         AND COALESCE(rating_count, 0) > 0) AS apv_rating_positive,
      COUNT(*) FILTER (WHERE gtin13 IS NOT NULL) AS apv_gtin13,
      COUNT(*) FILTER (WHERE LENGTH(COALESCE(description, '')) >= 200) AS apv_desc_ge_200,
      COUNT(*) FILTER (WHERE offers IS NOT NULL AND EXISTS (
          SELECT 1 FROM jsonb_array_elements(offers) AS o WHERE o ? 'seller_trust'
      )) AS apv_seller_trust_offers
    FROM agent_pdp_view
"""

# --- layer 2: the recommendation surface -----------------------------------
INDEX_SURFACE_SQL = """
    SELECT
      COUNT(*) AS pipeline_rows,
      COUNT(*) FILTER (WHERE serving_eligible IS TRUE) AS serving_eligible,
      COUNT(*) FILTER (WHERE index_eligible IS TRUE) AS index_eligible
    FROM index_pipeline_state
"""

INDEX_BLOCKERS_SQL = """
    SELECT blocker_code, COUNT(*) AS content_keys
    FROM index_pipeline_state
    WHERE serving_eligible IS DISTINCT FROM TRUE
    GROUP BY 1
    ORDER BY content_keys DESC
    LIMIT 8
"""

# --- layer 3: inputs -------------------------------------------------------
# INCI ingested: the seed-INCI backfill's own idempotency predicate.
INCI_INGESTED_SQL = """
    SELECT COUNT(DISTINCT product_key) AS inci_ingested_products
    FROM beauty_sku_ingredients
    WHERE COALESCE(raw_inci, '') <> ''
"""

# INCI captured but not necessarily ingested: the same validity gate
# backfill_seed_inci applies (>= 20 chars, >= 4 commas — marketing "key
# ingredients" bullets are not INCI).
INCI_CAPTURED_SQL = """
    SELECT COUNT(*) AS inci_captured_seeds
    FROM external_product_seeds s
    WHERE LENGTH(COALESCE(NULLIF(s.seed_data->>'inci_list', ''),
                          s.seed_data->>'pdp_ingredients_raw', '')) >= 20
      AND LENGTH(COALESCE(NULLIF(s.seed_data->>'inci_list', ''),
                          s.seed_data->>'pdp_ingredients_raw', ''))
        - LENGTH(REPLACE(COALESCE(NULLIF(s.seed_data->>'inci_list', ''),
                                  s.seed_data->>'pdp_ingredients_raw', ''),
                         ',', '')) >= 4
"""

ENRICHMENT_INPUT_SQL = """
    SELECT COUNT(*) AS enrichment_rows
    FROM product_enrichment
"""


async def collect() -> Dict[str, Any]:
    if not getattr(database, "is_connected", False):
        await database.connect()

    out: Dict[str, Any] = {}
    depth = dict(await database.fetch_one(APV_DEPTH_SQL) or {})
    out.update(depth)
    total = depth.get("apv_rows") or 0
    out["apv_desc_ge_200_pct"] = (
        round(100.0 * (depth.get("apv_desc_ge_200") or 0) / total, 1) if total else None
    )
    out.update(dict(await database.fetch_one(INDEX_SURFACE_SQL) or {}))
    out["top_blockers"] = [
        dict(r) for r in (await database.fetch_all(INDEX_BLOCKERS_SQL) or [])
    ]
    out.update(dict(await database.fetch_one(INCI_INGESTED_SQL) or {}))
    out.update(dict(await database.fetch_one(INCI_CAPTURED_SQL) or {}))
    out.update(dict(await database.fetch_one(ENRICHMENT_INPUT_SQL) or {}))
    return out


def _delta(now: Any, base: Optional[float]) -> str:
    if base is None or now is None:
        return "-"
    try:
        d = float(now) - float(base)
    except (TypeError, ValueError):
        return "-"
    return f"{d:+.1f}" if isinstance(base, float) and not float(base).is_integer() else f"{int(d):+d}"


def render(report: Dict[str, Any]) -> str:
    lines: List[str] = [
        f"=== agent depth scorecard — now vs {BASELINE_DATE} baseline ===",
        f"  {'metric':<28}{'now':>10}{'baseline':>10}{'delta':>8}  note",
    ]
    rows = [
        ("apv_rows", "served products"),
        ("apv_desc_ge_200_pct", "description >= 200 chars %"),
        ("apv_bullet_points", "bullet_points"),
        ("apv_usage_scenarios", "usage_scenarios"),
        ("apv_evidence_profile", "evidence_profile"),
        ("apv_required_disclaimers", "required_disclaimers"),
        ("apv_rating_positive", "rating (value + count>0)"),
        ("apv_gtin13", "gtin13"),
        ("apv_seller_trust_offers", "offers carrying seller_trust"),
        ("enrichment_rows", "product_enrichment rows"),
        ("inci_ingested_products", "INCI ingested (products)"),
        ("inci_captured_seeds", "INCI captured (seeds)"),
        ("serving_eligible", "serving_eligible"),
    ]
    for key, label in rows:
        now = report.get(key)
        base, note = BASELINES.get(key, (None, ""))
        base_s = "-" if base is None else (f"{base:g}")
        lines.append(
            f"  {label:<28}{str(now):>10}{base_s:>10}{_delta(now, base):>8}  {note}"
        )

    lines.append("\n=== recommendation surface ===")
    lines.append(f"  pipeline rows      {report.get('pipeline_rows')}")
    lines.append(f"  serving_eligible   {report.get('serving_eligible')}")
    lines.append(f"  index_eligible     {report.get('index_eligible')}")
    lines.append("  top blockers (not serving-eligible):")
    for b in report.get("top_blockers") or []:
        lines.append(f"    {str(b['blocker_code'])[:28]:<30}{b['content_keys']:>7}")
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
