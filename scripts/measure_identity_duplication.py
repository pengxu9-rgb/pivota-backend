#!/usr/bin/env python3
"""Standing identity duplication/fragmentation measurement (ADR-010 D-1).

Read-only. Quantifies the identity surface the canonical-product-identity
resolver (ADR-010) exists to manage, and serves as the ESCALATION GAUGE for
its deferred tiers: Tier 2/3 (embeddings/LLM adjudication) stay deferred until
these numbers demand them. Re-run monthly; attach output to the ADR thread.

First prod run 2026-07-09: products=10,999 merchants=8 content_keys=8,284;
cross-merchant shared keys=374 (1,513 rows, ~13.8% of catalog);
intra-merchant dup keys=1,076; pg groups=8,500 (3,609 memberships in
multi-member groups); active unattached seeds with an exact catalog URL
twin=249 (the Tier-0 attach population).

Usage (prod is Cloud Run, pivota-prod/us-west1; Railway is RETIRED (#1872) and its
database is a different one, so a duplication gauge read there measures a catalog
nobody is served from). There is no `railway run` equivalent — run a throwaway
job on the production image, which mounts the DATABASE_URL secret:
  scripts/ops/run_oneoff_job.sh scripts/measure_identity_duplication.py

Locally, against a database you already have a URL for:
  DATABASE_URL=... python scripts/measure_identity_duplication.py

Full pattern: docs/runbooks/operating_on_gcp_production.md.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.database import database  # noqa: E402

logger = logging.getLogger("measure_identity_duplication")

QUERIES: Dict[str, str] = {
    # Overall identity-key surface.
    "catalog_size": """
        SELECT COUNT(*) AS products,
               COUNT(DISTINCT merchant_id) AS merchants,
               COUNT(DISTINCT content_key) AS content_keys
        FROM catalog_products
        WHERE content_key IS NOT NULL
    """,
    # Same content_key under >1 merchant: the cross-merchant identity overlap
    # (true duplicates + brand-title collisions mixed — the resolver's input).
    "cross_merchant_shared_content_keys": """
        SELECT COUNT(*) AS keys, COALESCE(SUM(n_products), 0) AS products_involved
        FROM (
            SELECT content_key, COUNT(*) AS n_products
            FROM catalog_products
            WHERE content_key IS NOT NULL AND suppression_reason IS NULL
            GROUP BY content_key
            HAVING COUNT(DISTINCT merchant_id) > 1
        ) t
    """,
    # Same content_key repeated WITHIN one merchant (fragmentation and/or
    # variant families sharing brand+title — the two-grain signal).
    "same_merchant_dup_content_keys": """
        SELECT COUNT(*) AS keys
        FROM (
            SELECT content_key
            FROM catalog_products
            WHERE content_key IS NOT NULL AND suppression_reason IS NULL
            GROUP BY content_key, merchant_id
            HAVING COUNT(*) > 1
        ) t
    """,
    # Family-grain grouping reality: singleton vs multi-member pgs.
    "product_group_members": """
        SELECT COUNT(DISTINCT product_group_id) AS groups,
               COUNT(*) AS memberships,
               COUNT(*) FILTER (WHERE cnt > 1) AS multi_member_rows
        FROM (
            SELECT product_group_id,
                   COUNT(*) OVER (PARTITION BY product_group_id) AS cnt
            FROM product_group_members
        ) t
    """,
    # Tier-0 exact-attach population: active unattached seeds whose
    # canonical_url has an exact twin in the catalog (P1.3's target set).
    "seed_url_twins": """
        SELECT COUNT(*) AS seeds_with_catalog_url_twin
        FROM external_product_seeds s
        WHERE s.status = 'active'
          AND s.attached_product_key IS NULL
          AND EXISTS (
              SELECT 1 FROM catalog_products p
              WHERE p.canonical_url = s.canonical_url
                AND p.canonical_url IS NOT NULL
          )
    """,
}


async def run_measurement() -> Dict[str, List[Dict[str, Any]]]:
    out: Dict[str, List[Dict[str, Any]]] = {}
    for name, sql in QUERIES.items():
        try:
            out[name] = [dict(r) for r in await database.fetch_all(sql)]
        except Exception as exc:  # noqa: BLE001 — report per-query, keep going
            out[name] = [{"error": str(exc)[:200]}]
    return out


async def _main() -> Dict[str, Any]:
    if not getattr(database, "is_connected", False):
        await database.connect()
    try:
        return await run_measurement()
    finally:
        await database.disconnect()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    print(json.dumps(asyncio.run(_main()), indent=2, default=str))
