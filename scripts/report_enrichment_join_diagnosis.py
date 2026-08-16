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

# 5b. WHY the enriched-but-not-serving keys are blocked. index_pipeline_state
#     records a first-failing-check-wins `blocker_code` (migration 098 documents
#     the ladder). Enrichment is NOT one of the checks — the gate reads
#     description/image/price/quality — so a key blocked here is one the overlay
#     work cannot rescue on its own, and knowing WHICH check fails is what says
#     whether the remaining coverage gap is a content problem, a price/offer
#     problem, or an identity problem. The has_image / has_price / description
#     columns are carried alongside because the blocker names them.
COHORT_BLOCKERS_SQL = f"""
    WITH cohort AS (
      SELECT DISTINCT cp.content_key
      FROM product_enrichment pe
      JOIN catalog_products cp ON {_JOIN}
      WHERE cp.content_key IS NOT NULL AND pe.geo_code = 'default'
    )
    SELECT
      COALESCE(ips.blocker_code, '(no pipeline row)') AS blocker_code,
      COALESCE(ips.pipeline_stage, '(none)') AS pipeline_stage,
      COUNT(*) AS content_keys,
      COUNT(*) FILTER (WHERE ips.has_image IS TRUE) AS has_image,
      COUNT(*) FILTER (WHERE ips.has_price IS TRUE) AS has_price,
      COUNT(*) FILTER (WHERE COALESCE(ips.description_length, 0) >= 50) AS desc_ge_50,
      COUNT(*) FILTER (WHERE av.bullet_points IS NOT NULL
                          OR av.usage_scenarios IS NOT NULL) AS overlay_is_published
    FROM cohort c
    LEFT JOIN index_pipeline_state ips ON ips.content_key = c.content_key
    LEFT JOIN agent_pdp_view av ON av.content_key = c.content_key
    WHERE ips.serving_eligible IS DISTINCT FROM TRUE
    GROUP BY 1, 2
    ORDER BY content_keys DESC, blocker_code ASC
"""

# 5c. The individual blocked keys, so the breakdown is actionable rather than
#     just a histogram — blocker_detail carries the specific reason.
COHORT_BLOCKED_SAMPLES_SQL = f"""
    WITH cohort AS (
      SELECT DISTINCT cp.content_key, cp.brand, cp.title, cp.merchant_id
      FROM product_enrichment pe
      JOIN catalog_products cp ON {_JOIN}
      WHERE cp.content_key IS NOT NULL AND pe.geo_code = 'default'
    )
    SELECT
      c.content_key, c.brand, c.title, c.merchant_id,
      COALESCE(ips.blocker_code, '(no pipeline row)') AS blocker_code,
      LEFT(COALESCE(ips.blocker_detail, ''), 60) AS blocker_detail,
      COALESCE(ips.content_quality_score, -1) AS quality_score
    FROM cohort c
    LEFT JOIN index_pipeline_state ips ON ips.content_key = c.content_key
    WHERE ips.serving_eligible IS DISTINCT FROM TRUE
    ORDER BY blocker_code ASC, c.brand ASC NULLS LAST
    LIMIT 40
"""

# 5d. WHICH SCALE each blocked row's score is on. The gate compares against
#     QUALITY_SCORE_THRESHOLD = 71.4, raised from 65.0 in lockstep with dropping
#     the dead `summary` component (7 terms -> 6, every score rescaled by 7/6,
#     #1612). services/product_quality_service.py warns that stored scores stay
#     on the OLD scale until re-scored and that comparing the two "silently mixes
#     scales" — old-scale 61.2 is equivalent to new-scale 71.4.
#
#     So a row still carrying a pre-v3 rules_version is being measured against a
#     bar meant for the other scale, and could be demoted for arithmetic rather
#     than for content. Scores alone cannot answer this — 66.7 is both 4/6 (new)
#     and a plausible 7-term value — so read the recorded rules_version instead
#     of inferring from the number, which is exactly the mistake this query
#     exists to avoid.
COHORT_BLOCKED_SCALE_SQL = f"""
    WITH cohort AS (
      SELECT DISTINCT cp.content_key, cp.merchant_id, cp.platform,
             cp.source_product_id, cp.brand, cp.title
      FROM product_enrichment pe
      JOIN catalog_products cp ON {_JOIN}
      WHERE cp.content_key IS NOT NULL AND pe.geo_code = 'default'
    ),
    latest_snapshot AS (
      SELECT DISTINCT ON (merchant_id, platform, platform_product_id)
             merchant_id, platform, platform_product_id,
             rules_version, content_quality_score, snapshot_date
      FROM product_quality_snapshot
      ORDER BY merchant_id, platform, platform_product_id, snapshot_date DESC
    )
    SELECT
      c.brand, c.title,
      COALESCE(ips.blocker_code, '(no pipeline row)') AS blocker_code,
      ips.content_quality_score AS gate_score,
      COALESCE(q.rules_version, '(no snapshot)') AS rules_version,
      q.content_quality_score AS snapshot_score
    FROM cohort c
    LEFT JOIN index_pipeline_state ips ON ips.content_key = c.content_key
    LEFT JOIN latest_snapshot q
           ON q.merchant_id = c.merchant_id
          AND q.platform = c.platform
          AND q.platform_product_id = c.source_product_id
    WHERE ips.serving_eligible IS DISTINCT FROM TRUE
      -- 'not_scored' rides with 'low_quality' (2026-08-15 split). The
      -- report's cohort is 'blocked on content quality'; keying on
      -- 'low_quality' alone would silently drop every unscored row.
      AND COALESCE(ips.blocker_code, '') IN ('low_quality', 'not_scored')
    ORDER BY ips.content_quality_score DESC NULLS LAST
    LIMIT 40
"""

# 5e. CAN scripts/backfill_external_seed_quality_rescore even reach these keys?
#
#     The stale-scale finding (5d) suggests re-scoring the v1-lite rows. Before
#     running anything, check the tool actually applies: that script has NO
#     product selector — its flags are --apply/--limit/--tool-prefix/--force/
#     --include-eligible/--offset/--trust-flush-every/--skip-trust — so it
#     processes its whole candidate set, and `--limit N` takes an arbitrary
#     alphabetical slice (the candidate query is ORDER BY p.product_key). A
#     measured dry run reported 341 to-rescore, none of them ours.
#
#     It is also not merely a rescore: it runs make_external_seed_servable and
#     then upsert_catalog_row_trust, which flips catalog_row_trust.serving_decision
#     to `public` — the field public readers gate on.
#
#     So this reproduces that script's OWN candidate predicate per key, rather
#     than assuming. Each column is one conjunct of its WHERE clause plus the
#     `_rescored_ids()` skip, so a FALSE says exactly which conjunct excludes the
#     row. Reproduced from the script rather than eyeballed, because "the tool
#     does not apply here" is only worth reporting if it is checked.
RESCORE_REACHABILITY_SQL = f"""
    WITH cohort AS (
      SELECT DISTINCT cp.content_key, cp.product_key, cp.platform,
             cp.source_product_id, cp.source_ref, cp.brand, cp.title
      FROM product_enrichment pe
      JOIN catalog_products cp ON {_JOIN}
      WHERE cp.content_key IS NOT NULL AND pe.geo_code = 'default'
    )
    SELECT
      c.brand, c.title, c.platform,
      (c.platform = 'external_seed') AS platform_ok,
      EXISTS (SELECT 1 FROM external_product_seeds eps
              WHERE eps.attached_product_key = c.product_key) AS has_seed_attachment,
      (ips.content_key IS NOT NULL) AS has_pipeline_row,
      (ips.serving_eligible IS NOT TRUE) AS not_already_eligible,
      NOT EXISTS (
        SELECT 1 FROM product_quality_snapshot q
        WHERE q.platform_product_id = c.source_product_id
          AND q.rules_version = :current_rules_version
      ) AS not_already_rescored
    FROM cohort c
    LEFT JOIN index_pipeline_state ips ON ips.content_key = c.content_key
    WHERE ips.serving_eligible IS DISTINCT FROM TRUE
      -- 'not_scored' rides with 'low_quality' (2026-08-15 split). The
      -- report's cohort is 'blocked on content quality'; keying on
      -- 'low_quality' alone would silently drop every unscored row.
      AND COALESCE(ips.blocker_code, '') IN ('low_quality', 'not_scored')
    ORDER BY c.brand ASC NULLS LAST
    LIMIT 40
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
    out["cohort_blockers"] = [
        dict(r) for r in (await database.fetch_all(COHORT_BLOCKERS_SQL) or [])
    ]
    out["cohort_blocked_samples"] = [
        dict(r) for r in (await database.fetch_all(COHORT_BLOCKED_SAMPLES_SQL) or [])
    ]
    out["low_quality_scale"] = [
        dict(r) for r in (await database.fetch_all(COHORT_BLOCKED_SCALE_SQL) or [])
    ]
    # Import the constant the rescore script itself skips on, so this probe
    # cannot drift from the tool it is predicting.
    from services.product_quality_service import (
        SOURCE_BACKED_COMPONENTS_RULES_VERSION,
    )
    out["rescore_reachability"] = [
        dict(r) for r in (await database.fetch_all(
            RESCORE_REACHABILITY_SQL,
            {"current_rules_version": SOURCE_BACKED_COMPONENTS_RULES_VERSION},
        ) or [])
    ]
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

    lines.append("\n=== 5b. why the enriched cohort is NOT serving-eligible ===")
    lines.append(f"  {'blocker_code':<24}{'stage':<16}{'keys':>5}{'img':>5}"
                 f"{'price':>7}{'desc50':>8}{'published':>11}")
    if not report.get("cohort_blockers"):
        lines.append("  (none — every enriched key is serving-eligible)")
    for b in report.get("cohort_blockers") or []:
        lines.append(
            f"  {str(b['blocker_code'])[:23]:<24}{str(b['pipeline_stage'])[:15]:<16}"
            f"{b['content_keys']:>5}{b['has_image']:>5}{b['has_price']:>7}"
            f"{b['desc_ge_50']:>8}{b['overlay_is_published']:>11}"
        )

    lines.append("\n=== 5c. the blocked keys ===")
    for r in report.get("cohort_blocked_samples") or []:
        detail = f" | {r['blocker_detail']}" if r.get("blocker_detail") else ""
        lines.append(
            f"  {str(r['blocker_code'])[:20]:<21} {str(r['brand'])[:16]:<17} "
            f"{str(r['title'])[:38]:<39} q={r['quality_score']}{detail}"
        )

    lines.append("\n=== 5d. which SCALE each low_quality score is on "
                 "(gate compares against 71.4) ===")
    lines.append(f"  {'brand':<18}{'title':<34}{'gate':>7}{'snap':>7}  rules_version")
    rows = report.get("low_quality_scale") or []
    if not rows:
        lines.append("  (no low_quality keys)")
    for r in rows:
        gate = r.get("gate_score")
        snap = r.get("snapshot_score")
        lines.append(
            f"  {str(r.get('brand'))[:17]:<18}{str(r.get('title'))[:33]:<34}"
            f"{(round(gate, 1) if gate is not None else '-'):>7}"
            f"{(round(snap, 1) if snap is not None else '-'):>7}  {r.get('rules_version')}"
        )

    lines.append("\n=== 5e. can the rescore script reach the low_quality keys? ===")
    lines.append(f"  {'brand':<16}{'platform':<16}{'plat':>6}{'seed':>6}{'ips':>5}"
                 f"{'notElig':>9}{'notV3':>7}  REACHABLE")
    reach = report.get("rescore_reachability") or []
    if not reach:
        lines.append("  (no low_quality keys)")
    for r in reach:
        conjuncts = [r.get("platform_ok"), r.get("has_seed_attachment"),
                     r.get("has_pipeline_row"), r.get("not_already_eligible"),
                     r.get("not_already_rescored")]
        def y(v): return "Y" if v else "n"
        lines.append(
            f"  {str(r.get('brand'))[:15]:<16}{str(r.get('platform'))[:15]:<16}"
            f"{y(conjuncts[0]):>6}{y(conjuncts[1]):>6}{y(conjuncts[2]):>5}"
            f"{y(conjuncts[3]):>9}{y(conjuncts[4]):>7}  "
            f"{'YES' if all(conjuncts) else 'no'}"
        )

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
