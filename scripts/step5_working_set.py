#!/usr/bin/env python3
"""Step-5 Lane 0 — catalog identity reconciliation working set (read-only).

Implements Lane 0 of docs/plans/adr011_step5_catalog_identity_reconciliation.md:
re-derives the same-merchant duplicate / cross-merchant shared content_key
backlog with the exclusions later lanes must not double-count —

  - suppressed rows (suppression_reason IS NOT NULL) are never fetched;
  - external_seed rows whose seed is deactivated or missing (the two-mirror
    gotcha: deactivating a seed does NOT tombstone its catalog_products
    mirror) are pulled OUT of the lane populations and reported separately
    as `orphan_mirrors`, the input to an explicit suppression sweep;
  - demo-store rows (pivota-review-demo*) are excluded and counted.

The remainder is classified into the plan's lanes:

  lane1_duplicate_store_connection  cross-merchant keys where >=2 merchants
                                    share a source_product_id (same store
                                    connected under two merchant accounts)
  lane4_seed_first_party_twin       cross-merchant keys mixing external_seed
                                    with a first-party platform
  lane4_review_cross_merchant       any other cross-merchant key
  lane2_same_url                    same-merchant groups where EVERY row has
                                    the same normalized canonical_url
                                    (querystring/UTM/#fragment noise included)
  lane3_campaign_clones             same-merchant, one domain (source_domain,
                                    else the URL host), several normalized
                                    URLs (campaign-slug clones)
  lane4_no_url_signal               same-merchant, no row has a canonical_url
                                    (e.g. first-party shopify brand+title
                                    families) — review, or cleared by Lane 1
  lane4_multi_domain                same-merchant, rows span >1 domain
  lane4_mixed_url_presence          same-merchant, some rows lack a URL —
                                    too weakly evidenced to auto-collapse

This script only reads. It is the single source of truth for lanes 1-4 and is
re-run after each lane's apply cut to show convergence (alongside the D-1
gauge scripts/measure_identity_duplication.py, whose group definitions it
mirrors: content_key IS NOT NULL AND suppression_reason IS NULL).

Run against prod via railway (public proxy; the pooled database.connect()
flakes on it, so this uses a single asyncpg connection with retries — see
docs/adr/ADR-011-rollout-handoff.md §6):

  railway run bash -c 'DATABASE_URL="$DATABASE_PUBLIC_URL" PYTHONPATH="$PWD" \
      python3.11 scripts/step5_working_set.py --output-json reports/step5/working_set.json'
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.pdp_matcher.deterministic import normalize_canonical_url  # noqa: E402

DEMO_DOMAIN_PREFIX = "pivota-review-demo"

# Seed linkage is bidirectional: the mirror door stamps the seed id into
# catalog_products.source_ref, but the enrichment door (source_system
# catalog_enrichment_agent_v1) writes NO source_ref — its rows are linked the
# other way, via external_product_seeds.attached_product_key. A row only
# counts as seed-less ('missing') when NEITHER direction finds a seed.
SEED_STATUS_SQL = """
    CASE WHEN cp.platform = 'external_seed' THEN
        CASE
            WHEN EXISTS (
                SELECT 1 FROM external_product_seeds e
                WHERE (e.id = cp.source_ref
                       OR e.attached_product_key = cp.product_key)
                  AND lower(coalesce(e.status, '')) = 'active')
            THEN 'active'
            WHEN EXISTS (
                SELECT 1 FROM external_product_seeds e
                WHERE e.id = cp.source_ref
                   OR e.attached_product_key = cp.product_key)
            THEN 'inactive'
            ELSE 'missing'
        END
    END
"""

WORKING_ROWS_SQL = f"""
WITH keyed AS (
    SELECT cp.merchant_id, cp.content_key, cp.product_key, cp.platform,
           cp.canonical_url, cp.source_domain, cp.source_product_id,
           cp.source_ref, cp.pivota_signature_id, cp.title, cp.created_at,
           {SEED_STATUS_SQL} AS seed_status
    FROM catalog_products cp
    WHERE cp.content_key IS NOT NULL AND cp.suppression_reason IS NULL
),
xm AS (
    SELECT content_key FROM keyed
    GROUP BY 1 HAVING COUNT(DISTINCT merchant_id) > 1
),
sm AS (
    SELECT merchant_id, content_key FROM keyed
    GROUP BY 1, 2 HAVING COUNT(*) > 1
)
SELECT k.* FROM keyed k
LEFT JOIN xm ON xm.content_key = k.content_key
LEFT JOIN sm ON sm.merchant_id = k.merchant_id AND sm.content_key = k.content_key
WHERE xm.content_key IS NOT NULL OR sm.content_key IS NOT NULL
"""

# Whole-catalog orphan-mirror population (not just dup groups): every active
# external_seed catalog row with no active seed in EITHER linkage direction
# (see SEED_STATUS_SQL). Shared with scripts/step5_sweep_orphan_mirrors.py so
# the report and the sweep can never disagree about what an orphan mirror is.
ORPHAN_MIRRORS_SQL = f"""
SELECT cp.product_key, cp.content_key, cp.source_ref, cp.canonical_url,
       cp.pivota_signature_id,
       {SEED_STATUS_SQL} AS seed_status
FROM catalog_products cp
WHERE cp.platform = 'external_seed'
  AND cp.suppression_reason IS NULL
  AND NOT EXISTS (
        SELECT 1 FROM external_product_seeds e
        WHERE (e.id = cp.source_ref
               OR e.attached_product_key = cp.product_key)
          AND lower(coalesce(e.status, '')) = 'active')
"""


def is_demo_row(row: Dict[str, Any]) -> bool:
    return str(row.get("source_domain") or "").startswith(DEMO_DOMAIN_PREFIX)


def is_orphan_mirror_row(row: Dict[str, Any]) -> bool:
    """External_seed row whose backing seed is not active ('missing' when the
    seed id doesn't resolve). Non-seed rows have seed_status None."""
    status = row.get("seed_status")
    return status is not None and status != "active"


def _row_detail(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "product_key": row.get("product_key"),
        "merchant_id": row.get("merchant_id"),
        "platform": row.get("platform"),
        "canonical_url": row.get("canonical_url"),
        "normalized_url": normalize_canonical_url(row.get("canonical_url")),
        "source_domain": row.get("source_domain"),
        "source_product_id": row.get("source_product_id"),
        "seed_status": row.get("seed_status"),
        "has_signature": bool(row.get("pivota_signature_id")),
        "created_at": str(row.get("created_at") or ""),
    }


def classify_cross_merchant_group(rows: List[Dict[str, Any]]) -> str:
    """Lane for one cross-merchant content_key group (rows span >1 merchant)."""
    spid_merchants: Dict[str, set] = defaultdict(set)
    for r in rows:
        spid = str(r.get("source_product_id") or "").strip()
        if spid:
            spid_merchants[spid].add(r.get("merchant_id"))
    if any(len(m) > 1 for m in spid_merchants.values()):
        return "lane1_duplicate_store_connection"
    platforms = {r.get("platform") for r in rows}
    if "external_seed" in platforms and len(platforms) > 1:
        return "lane4_seed_first_party_twin"
    return "lane4_review_cross_merchant"


def _effective_domain(row: Dict[str, Any]) -> str:
    """source_domain, falling back to the normalized canonical_url's host
    (many external_seed rows carry a URL but a NULL source_domain)."""
    domain = str(row.get("source_domain") or "").strip()
    if domain:
        return domain
    normalized = normalize_canonical_url(row.get("canonical_url"))
    if normalized:
        host = normalized.split("://", 1)[1]
        return host.split("/", 1)[0]
    return ""


def classify_same_merchant_group(rows: List[Dict[str, Any]]) -> str:
    """Lane for one same-merchant dup group (>=2 rows, one merchant+key)."""
    normalized = [normalize_canonical_url(r.get("canonical_url")) for r in rows]
    if not any(normalized):
        return "lane4_no_url_signal"
    if not all(normalized):
        # A blank-URL row is not provably the same PDP as its URL-bearing
        # twins — too weakly evidenced for the mechanical lanes.
        return "lane4_mixed_url_presence"
    if len(set(normalized)) == 1:
        return "lane2_same_url"
    domains = {_effective_domain(r) for r in rows}
    domains.discard("")
    if len(domains) == 1:
        return "lane3_campaign_clones"
    return "lane4_multi_domain"


def build_report(
    working_rows: List[Dict[str, Any]],
    orphan_rows: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Pure classification of the fetched rows into the Lane-0 report."""
    demo = [r for r in working_rows if is_demo_row(r)]
    orphan_in_groups = [
        r for r in working_rows if not is_demo_row(r) and is_orphan_mirror_row(r)
    ]
    active = [
        r for r in working_rows if not is_demo_row(r) and not is_orphan_mirror_row(r)
    ]

    by_key: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    by_merchant_key: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    for r in active:
        by_key[r["content_key"]].append(r)
        by_merchant_key[(r["merchant_id"], r["content_key"])].append(r)

    lanes: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for content_key, rows in sorted(by_key.items()):
        if len({r["merchant_id"] for r in rows}) < 2:
            continue
        lanes[classify_cross_merchant_group(rows)].append(
            {
                "content_key": content_key,
                "merchants": sorted({r["merchant_id"] for r in rows}),
                "rows": [_row_detail(r) for r in rows],
            }
        )
    for (merchant_id, content_key), rows in sorted(by_merchant_key.items()):
        if len(rows) < 2:
            continue
        lanes[classify_same_merchant_group(rows)].append(
            {
                "merchant_id": merchant_id,
                "content_key": content_key,
                "rows": [_row_detail(r) for r in rows],
            }
        )

    summary = {
        lane: {
            "groups": len(groups),
            "rows": sum(len(g["rows"]) for g in groups),
        }
        for lane, groups in sorted(lanes.items())
    }
    summary["excluded_demo"] = {"rows": len(demo)}
    summary["excluded_orphan_mirrors_in_groups"] = {"rows": len(orphan_in_groups)}
    summary["orphan_mirrors_catalog_wide"] = {"rows": len(orphan_rows)}

    return {
        "summary": summary,
        "lanes": dict(lanes),
        "orphan_mirrors": [
            {
                "product_key": r.get("product_key"),
                "content_key": r.get("content_key"),
                "source_ref": r.get("source_ref"),
                "canonical_url": r.get("canonical_url"),
                "seed_status": r.get("seed_status"),
            }
            for r in orphan_rows
        ],
        "excluded_demo_rows": [_row_detail(r) for r in demo],
    }


async def _connect_with_retry(dsn: str, attempts: int = 6):
    import asyncpg

    last: Optional[Exception] = None
    for i in range(attempts):
        try:
            return await asyncpg.connect(dsn, timeout=30, command_timeout=180)
        except Exception as e:  # public proxy flakes intermittently
            last = e
            await asyncio.sleep(2 * (i + 1))
    raise last  # type: ignore[misc]


async def _fetch() -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    conn = await _connect_with_retry(os.environ["DATABASE_URL"])
    try:
        working = [dict(r) for r in await conn.fetch(WORKING_ROWS_SQL)]
        orphans = [dict(r) for r in await conn.fetch(ORPHAN_MIRRORS_SQL)]
        return working, orphans
    finally:
        await conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-json",
        help="Write the full report (group lists, orphan mirrors) to this path",
    )
    args = parser.parse_args()

    working, orphans = asyncio.run(_fetch())
    report = build_report(working, orphans)

    if args.output_json:
        os.makedirs(os.path.dirname(args.output_json) or ".", exist_ok=True)
        with open(args.output_json, "w") as fh:
            json.dump(report, fh, indent=2, default=str)
        print(f"full report -> {args.output_json}", file=sys.stderr)

    print(json.dumps({"summary": report["summary"]}, indent=2, default=str))


if __name__ == "__main__":
    main()
