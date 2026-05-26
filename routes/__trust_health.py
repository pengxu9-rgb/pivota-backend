"""catalog_row_trust health endpoint — Layer C1 observability.

Read-only. Reports the current state of the catalog_row_trust table so
operators can confirm the dual-write hooks and backfill cron are keeping
the table current.

Mounted at GET /__trust_health (double-underscore marks internal/ops).
200 in all cases; the response shape carries the verdict.
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter

router = APIRouter(tags=["trust-health"])

_STATS_SQL = """
    WITH decision_counts AS (
        SELECT serving_decision, count(*) AS cnt
        FROM catalog_row_trust
        GROUP BY serving_decision
    ),
    version_counts AS (
        SELECT policy_version, count(*) AS cnt
        FROM catalog_row_trust
        GROUP BY policy_version
    ),
    stale_count AS (
        SELECT count(*) AS cnt
        FROM catalog_row_trust
        WHERE updated_at < now() - interval '24 hours'
    ),
    total AS (
        SELECT count(*) AS cnt FROM catalog_row_trust
    ),
    catalog_total AS (
        SELECT count(*) AS cnt FROM catalog_products
    ),
    drift AS (
        SELECT count(*) AS cnt
        FROM catalog_products cp
        WHERE NOT EXISTS (
            SELECT 1 FROM catalog_row_trust crt
            WHERE crt.subject_type = 'product'
              AND crt.subject_key  = cp.product_key
        )
    )
    SELECT
        (SELECT cnt FROM total)         AS total_trust_rows,
        (SELECT cnt FROM catalog_total) AS total_catalog_rows,
        (SELECT cnt FROM drift)         AS catalog_rows_without_trust,
        (SELECT cnt FROM stale_count)   AS stale_rows_24h,
        (SELECT json_object_agg(serving_decision, cnt) FROM decision_counts) AS decision_distribution,
        (SELECT json_object_agg(policy_version, cnt)  FROM version_counts)   AS version_distribution
"""


@router.get("/__trust_health")
async def trust_health() -> Dict[str, Any]:
    """Report catalog_row_trust table health.

    Returns:
        total_trust_rows: rows in catalog_row_trust
        total_catalog_rows: rows in catalog_products (product-type source of truth)
        catalog_rows_without_trust: catalog_products with no matching trust row (drift)
        stale_rows_24h: trust rows not updated in the last 24 hours
        decision_distribution: {public, shadow, blocked, ...} counts
        version_distribution: {policy_version: count} map
    """
    try:
        from db.database import database

        row = await database.fetch_one(_STATS_SQL)
        if row is None:
            return {"error": "no rows returned from stats query"}

        return {
            "total_trust_rows": int(row["total_trust_rows"] or 0),
            "total_catalog_rows": int(row["total_catalog_rows"] or 0),
            "catalog_rows_without_trust": int(row["catalog_rows_without_trust"] or 0),
            "stale_rows_24h": int(row["stale_rows_24h"] or 0),
            "decision_distribution": row["decision_distribution"] or {},
            "version_distribution": row["version_distribution"] or {},
        }
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}
