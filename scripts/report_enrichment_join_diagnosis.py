"""WHY do product_enrichment rows fail to join catalog_products — measured, per row.

The 2026-08-11 baseline (`report_enrichment_propagation_baseline.py`) found that
only 112 of 360 product_enrichment rows join catalog_products on the identity
triple, and that all 109 resolvable content_keys are already serving their
overlay. That leaves three open questions this report answers with counts
instead of hypotheses:

  1. WHY don't the other 248 join? Each non-joining row is classified into
     exactly one bucket: the merchant has no catalog rows at all / the merchant
     exists but never on this platform / merchant+platform exist but this
     product id does not. Orthogonally: does the row join `products_cache`
     instead? The enrichment domain was BUILT against products_cache
     (db/product_enrichment.py: "Acts as an overlay on top of products_cache"),
     and `jobs/product_enrichment_worker.py` iterates products_cache — so
     "joins the cache but not the catalog" is the signature of enrichment
     written for products that were never catalog-synced.

  2. Is the enriched content actually in the RECOMMENDATION surface?
     `routes/agent_recommendations.py` proxies to the internal Recommendations
     service, which serves from this backend's surfaces — so the measurable
     backend-side answer is: of the enriched cohort, how many content_keys are
     serving_eligible / index_eligible / carry a signature. Plus the inverse:
     agent_pdp_view rows SERVING an overlay whose enrichment row no longer
     resolves (frozen copies the publish bridge can never refresh).

  3. Will FUTURE enrichment join? The monthly join-rate trend answers it
     empirically: if recent writes join and old ones do not, the problem is
     historical; if recent writes also miss, the writer is still producing
     unjoinable identity.

Read-only: every statement is a SELECT; there is no --apply. The
nothing-to-build probe reuses the REAL `fetch_products_for_key` (with its
source-quarantine anti-join) so the answer reflects the serving path, not a
retyped approximation.

Usage
-----
  python3 scripts/report_enrichment_join_diagnosis.py
  python3 scripts/report_enrichment_join_diagnosis.py --json
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

# The identity join, spelled once. pe.platform_product_id IS
# cp.source_product_id — the spelling mismatch that killed the on-write bridge.
_JOIN = (
    "cp.merchant_id = pe.merchant_id "
    "AND cp.platform = pe.platform "
    "AND cp.source_product_id = pe.platform_product_id"
)

# 1. One bucket per non-joining row, most-specific-first. The three EXISTS
#    buckets partition the set; the two products_cache counts are orthogonal.
CLASSIFY_SQL = f"""
    SELECT
      COUNT(*) AS nonjoining_total,
      COUNT(*) FILTER (WHERE NOT EXISTS (
          SELECT 1 FROM catalog_products cp WHERE cp.merchant_id = pe.merchant_id
      )) AS merchant_absent_from_catalog,
      COUNT(*) FILTER (WHERE EXISTS (
          SELECT 1 FROM catalog_products cp WHERE cp.merchant_id = pe.merchant_id
      ) AND NOT EXISTS (
          SELECT 1 FROM catalog_products cp
          WHERE cp.merchant_id = pe.merchant_id AND cp.platform = pe.platform
      )) AS platform_mismatch,
      COUNT(*) FILTER (WHERE EXISTS (
          SELECT 1 FROM catalog_products cp
          WHERE cp.merchant_id = pe.merchant_id AND cp.platform = pe.platform
      )) AS id_missing_within_merchant_platform,
      COUNT(*) FILTER (WHERE EXISTS (
          SELECT 1 FROM products_cache pc
          WHERE pc.merchant_id = pe.merchant_id
            AND pc.platform = pe.platform
            AND pc.platform_product_id = pe.platform_product_id
      )) AS joins_products_cache_instead,
      COUNT(*) FILTER (WHERE NOT EXISTS (
          SELECT 1 FROM products_cache pc
          WHERE pc.merchant_id = pe.merchant_id
            AND pc.platform = pe.platform
            AND pc.platform_product_id = pe.platform_product_id
      )) AS joins_nothing_at_all
    FROM product_enrichment pe
    WHERE NOT EXISTS (SELECT 1 FROM catalog_products cp WHERE {_JOIN})
"""

# 2. Which merchants/platforms hold the non-joining rows.
BY_MERCHANT_SQL = f"""
    SELECT
      pe.merchant_id,
      pe.platform,
      COUNT(*) AS nonjoining_rows,
      EXISTS (SELECT 1 FROM catalog_products cp
              WHERE cp.merchant_id = pe.merchant_id) AS merchant_in_catalog,
      EXISTS (SELECT 1 FROM catalog_products cp
              WHERE cp.merchant_id = pe.merchant_id
                AND cp.platform = pe.platform) AS platform_in_catalog,
      COUNT(*) FILTER (WHERE EXISTS (
          SELECT 1 FROM products_cache pc
          WHERE pc.merchant_id = pe.merchant_id
            AND pc.platform = pe.platform
            AND pc.platform_product_id = pe.platform_product_id
      )) AS in_products_cache
    FROM product_enrichment pe
    WHERE NOT EXISTS (SELECT 1 FROM catalog_products cp WHERE {_JOIN})
    GROUP BY pe.merchant_id, pe.platform
    ORDER BY nonjoining_rows DESC, pe.merchant_id ASC
    LIMIT 15
"""

# 3. Id-shape samples for one merchant+platform, to make an id-space mismatch
#    visible to the eye (gid:// forms, prefixes, handles-vs-numerics).
SAMPLE_ENRICHMENT_IDS_SQL = f"""
    SELECT pe.platform_product_id
    FROM product_enrichment pe
    WHERE pe.merchant_id = :merchant_id AND pe.platform = :platform
      AND NOT EXISTS (SELECT 1 FROM catalog_products cp WHERE {_JOIN})
    ORDER BY pe.platform_product_id
    LIMIT 5
"""

SAMPLE_CATALOG_IDS_SQL = """
    SELECT cp.source_product_id
    FROM catalog_products cp
    WHERE cp.merchant_id = :merchant_id AND cp.platform = :platform
    ORDER BY cp.source_product_id
    LIMIT 5
"""

# 4. The future-proofing evidence: join rate by month of last write.
TREND_SQL = f"""
    SELECT
      to_char(date_trunc('month', COALESCE(pe.updated_at, pe.created_at)),
              'YYYY-MM') AS month,
      COUNT(*) AS rows,
      COUNT(*) FILTER (WHERE EXISTS (
          SELECT 1 FROM catalog_products cp WHERE {_JOIN}
      )) AS joining
    FROM product_enrichment pe
    GROUP BY 1
    ORDER BY 1
"""

# 5. The recommendation-surface status of the JOINABLE enriched cohort.
COHORT_STATUS_SQL = f"""
    WITH cohort AS (
      SELECT DISTINCT cp.content_key
      FROM product_enrichment pe
      JOIN catalog_products cp ON {_JOIN}
      WHERE cp.content_key IS NOT NULL AND pe.geo_code = 'default'
    )
    SELECT
      COUNT(*) AS cohort_content_keys,
      COUNT(*) FILTER (WHERE ips.serving_eligible IS TRUE) AS serving_eligible,
      COUNT(*) FILTER (WHERE ips.index_eligible IS TRUE) AS index_eligible,
      COUNT(*) FILTER (WHERE ips.content_key IS NULL) AS no_pipeline_row,
      COUNT(*) FILTER (WHERE av.pivota_signature_id IS NOT NULL) AS has_signature
    FROM cohort c
    LEFT JOIN index_pipeline_state ips ON ips.content_key = c.content_key
    LEFT JOIN agent_pdp_view av ON av.content_key = c.content_key
"""

# 6. The inverse: rows SERVING an overlay whose enrichment no longer resolves.
#    The publish bridge can never refresh these — they are frozen copies.
ORPHANED_SERVING_SQL = f"""
    WITH cohort AS (
      SELECT DISTINCT cp.content_key
      FROM product_enrichment pe
      JOIN catalog_products cp ON {_JOIN}
      WHERE cp.content_key IS NOT NULL AND pe.geo_code = 'default'
    )
    SELECT
      COUNT(*) AS orphaned_serving_rows,
      COUNT(*) FILTER (WHERE ips.serving_eligible IS TRUE) AS serving_eligible,
      COUNT(*) FILTER (WHERE av.pivota_signature_id IS NOT NULL) AS has_signature
    FROM agent_pdp_view av
    LEFT JOIN index_pipeline_state ips ON ips.content_key = av.content_key
    WHERE (av.bullet_points IS NOT NULL OR av.usage_scenarios IS NOT NULL)
      AND NOT EXISTS (SELECT 1 FROM cohort c WHERE c.content_key = av.content_key)
"""

# 7a. The joinable cohort keys, for the nothing-to-build probe below.
COHORT_KEYS_SQL = f"""
    SELECT DISTINCT cp.content_key
    FROM product_enrichment pe
    JOIN catalog_products cp ON {_JOIN}
    WHERE cp.content_key IS NOT NULL AND pe.geo_code = 'default'
    ORDER BY cp.content_key ASC
"""

# 7b. What a nothing-to-build key looks like WITHOUT the quarantine anti-join,
#     so the report can say what got filtered rather than just that it was.
PLAIN_PRODUCT_SQL = """
    SELECT cp.brand, cp.title, cp.merchant_id, cp.platform
    FROM catalog_products cp
    WHERE cp.content_key = :ck
    ORDER BY cp.product_key ASC
    LIMIT 1
"""


async def collect() -> Dict[str, Any]:
    if not getattr(database, "is_connected", False):
        await database.connect()

    out: Dict[str, Any] = {}
    out["classification"] = dict(await database.fetch_one(CLASSIFY_SQL) or {})
    out["by_merchant_platform"] = [
        dict(r) for r in (await database.fetch_all(BY_MERCHANT_SQL) or [])
    ]

    # Id-shape samples for the top 3 offender groups whose merchant+platform DO
    # exist in the catalog — only there can two id spellings sit side by side.
    samples: List[Dict[str, Any]] = []
    for group in out["by_merchant_platform"][:6]:
        if not group.get("platform_in_catalog") or len(samples) >= 3:
            continue
        params = {"merchant_id": group["merchant_id"], "platform": group["platform"]}
        samples.append({
            "merchant_id": group["merchant_id"],
            "platform": group["platform"],
            "enrichment_ids": [
                r["platform_product_id"]
                for r in await database.fetch_all(SAMPLE_ENRICHMENT_IDS_SQL, params)
            ],
            "catalog_ids": [
                r["source_product_id"]
                for r in await database.fetch_all(SAMPLE_CATALOG_IDS_SQL, params)
            ],
        })
    out["id_shape_samples"] = samples

    out["join_rate_by_month"] = [
        dict(r) for r in (await database.fetch_all(TREND_SQL) or [])
    ]
    out["cohort_recommendation_status"] = dict(
        await database.fetch_one(COHORT_STATUS_SQL) or {}
    )
    out["orphaned_serving"] = dict(
        await database.fetch_one(ORPHANED_SERVING_SQL) or {}
    )

    # The nothing-to-build keys, via the REAL serving read path (quarantine
    # anti-join included). A key listed here joined the cohort but assembles to
    # nothing — the plain row alongside shows what the anti-join removed.
    from services.agent_pdp_view_assembler import fetch_products_for_key

    nothing_to_build: List[Dict[str, Any]] = []
    cohort_keys = [
        r["content_key"] for r in (await database.fetch_all(COHORT_KEYS_SQL) or [])
    ]
    for ck in cohort_keys:
        products = await fetch_products_for_key(ck)
        if products:
            continue
        plain = await database.fetch_one(PLAIN_PRODUCT_SQL, {"ck": ck})
        nothing_to_build.append({
            "content_key": ck,
            **({k: plain[k] for k in ("brand", "title", "merchant_id", "platform")}
               if plain else {"note": "no catalog row even without the anti-join"}),
        })
    out["nothing_to_build"] = nothing_to_build
    out["nothing_to_build_count"] = len(nothing_to_build)
    return out


def render(report: Dict[str, Any]) -> str:
    lines: List[str] = ["=== 1. why the non-joining rows do not join ==="]
    for k, v in report["classification"].items():
        lines.append(f"  {k:<40} {v}")

    lines.append("\n=== 2. non-joining rows by merchant/platform (top 15) ===")
    lines.append(f"  {'merchant_id':<28}{'platform':<12}{'rows':>5}"
                 f"{'m_in_cat':>10}{'p_in_cat':>10}{'in_cache':>10}")
    for g in report["by_merchant_platform"]:
        lines.append(
            f"  {str(g['merchant_id'])[:27]:<28}{str(g['platform'])[:11]:<12}"
            f"{g['nonjoining_rows']:>5}{str(g['merchant_in_catalog']):>10}"
            f"{str(g['platform_in_catalog']):>10}{g['in_products_cache']:>10}"
        )

    lines.append("\n=== 3. id-shape samples (enrichment vs catalog, same merchant+platform) ===")
    if not report["id_shape_samples"]:
        lines.append("  (no offender group has its merchant+platform in the catalog)")
    for s in report["id_shape_samples"]:
        lines.append(f"  {s['merchant_id']} / {s['platform']}:")
        lines.append(f"    enrichment ids: {s['enrichment_ids']}")
        lines.append(f"    catalog ids   : {s['catalog_ids']}")

    lines.append("\n=== 4. join rate by month of last enrichment write ===")
    for r in report["join_rate_by_month"]:
        lines.append(f"  {r['month']}: {r['joining']}/{r['rows']} join")

    lines.append("\n=== 5. joinable cohort in the recommendation surface ===")
    for k, v in report["cohort_recommendation_status"].items():
        lines.append(f"  {k:<40} {v}")

    lines.append("\n=== 6. serving an overlay whose enrichment no longer resolves ===")
    for k, v in report["orphaned_serving"].items():
        lines.append(f"  {k:<40} {v}")

    lines.append(f"\n=== 7. cohort keys that assemble to NOTHING on the real read path "
                 f"({report['nothing_to_build_count']}) ===")
    for row in report["nothing_to_build"][:20]:
        desc = row.get("note") or (
            f"{row.get('brand')} | {str(row.get('title'))[:44]} | "
            f"{row.get('merchant_id')}/{row.get('platform')}"
        )
        lines.append(f"  {row['content_key']}: {desc}")
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
