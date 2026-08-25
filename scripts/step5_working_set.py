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
  - demo-store rows are excluded and counted — by storefront domain
    (pivota-review-demo*) and, for rigs with no domain to match on, by
    merchant_id (DEMO_MERCHANT_IDS).

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

Run against prod. Production is Cloud Run (pivota-prod/us-west1) since the
2026-08-22 cutover; Railway is the ROLLBACK, and its database is a different one,
so a convergence report taken there describes rows nobody is served from.

There is no `railway run` equivalent. Use a throwaway job on the production image
(the helper mounts the DATABASE_URL secret — a job inherits NO env and NO
secrets — and takes its verdict from the job's EXIT CODE):

  scripts/ops/run_oneoff_job.sh scripts/step5_working_set.py > working_set.txt

Do NOT pass --output-json to the job: it writes inside the container, whose
filesystem is destroyed on exit, and nothing retrieves it — the flag would appear
to work and produce nothing. The helper streams the run's stdout back, so
redirect THAT when you want a file. Full pattern:
docs/runbooks/operating_on_gcp_production.md.

(This still uses a single asyncpg connection with retries rather than the pooled
database.connect(), which flaked against the old public proxy — see
docs/adr/ADR-011-rollout-handoff.md §6.)
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

# Demo/test-track stores: the pivota-review-demo pair is the LIVE Shopify
# app-review rig — its rows must keep serving, so we deliberately do NOT
# relabel their catalog_track (that column is a serving-classification enum:
# 'internal_merchant' drives first-party offer typing in
# services/offer_classification.py). Demo-ness is instead THIS one shared
# predicate, consumed by the working-set classifier and the sweep gauges —
# add new demo storefronts here (or, for a rig with no storefront domain,
# to DEMO_MERCHANT_IDS just below), nowhere else.
DEMO_DOMAIN_PREFIX = "pivota-review-demo"

# Rigs that have no storefront domain to match on, so the prefix above can
# never reach them. Kept in lockstep with the two serving-side twins
# (services/test_merchant_policy.py KNOWN_TEST_MERCHANT_IDS and the agent
# repo's src/services/testMerchantPolicy.js TEST_MERCHANT_IDS).
#
# merch_test_ownist_001 — "Ownist Test Merchant", a seeded fixture catalog
# rather than a connected store: all 4 of its rows carry source_system
# 'ownist_test_fixture_v1' and all 4 are serving_eligible = TRUE. It has NO
# merchant_stores row and NO source_domain (verified in prod 2026-07-27), so
# the domain prefix is structurally blind to it. Found by the ADR-018
# connection-layer census (#1595); see docs/adr/ADR-018-connection-layer-and-
# priced-serving-lane.md.
#
# Scope note: this set deliberately does NOT list the two serving-side rigs
# that step5 has always counted (merch_efbc46b4619cfbdf and the snowboard
# ids beyond the pivota-review-demo domain match). Adding them would move
# ~1.5k rows out of the reconciliation backlog and re-baseline every gauge —
# a separate, measured decision, not a side effect of this exclusion.
DEMO_MERCHANT_IDS = frozenset({"merch_test_ownist_001"})

# SQL twin of is_demo_row(); interpolate into any gauge that should read the
# production catalog only. Derived from the constants above so the SQL and the
# Python predicate cannot drift apart. Both legs are alias-free because every
# consumer interpolates this into an unaliased `FROM catalog_products`, and the
# whole thing is parenthesised so it stays correct next to an OR.
def build_demo_exclusion_sql(prefix: str, merchant_ids: Any) -> str:
    """Render the demo-exclusion predicate from the two constants.

    The id leg is omitted entirely when the set is empty: `merchant_id NOT IN ()`
    is a Postgres syntax error, and because this is built at import time the
    module would still load cleanly — the break would only surface later, at
    query time, in the identity_reconcile_sweep gauges.
    """
    domain_leg = "COALESCE(source_domain, '') NOT LIKE '" + prefix + "%'"
    if not merchant_ids:
        return "(" + domain_leg + ")"
    id_list = ", ".join(
        "'" + str(mid).replace("'", "''") + "'" for mid in sorted(merchant_ids)
    )
    return "(" + domain_leg + " AND merchant_id NOT IN (" + id_list + "))"


DEMO_EXCLUSION_SQL = build_demo_exclusion_sql(DEMO_DOMAIN_PREFIX, DEMO_MERCHANT_IDS)

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
    """Demo/rig row: a demo storefront domain, OR a rig merchant_id that has no
    domain to match on (see DEMO_MERCHANT_IDS)."""
    if str(row.get("source_domain") or "").startswith(DEMO_DOMAIN_PREFIX):
        return True
    return str(row.get("merchant_id") or "").strip() in DEMO_MERCHANT_IDS


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
    parser = argparse.ArgumentParser(description=__doc__,    formatter_class=argparse.RawDescriptionHelpFormatter,)
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
