"""Pre-pilot readiness probe (Workstream C).

Before trusting a real merchant's first v3 audit, assert the audit's data
dependencies are actually present. Onboarding populates these via the WS-A
chain (merchant Sync → catalog ingest → quality backfill), but this probe is the
safety net: run it on a merchant and it tells you whether the audit will return a
real (non-blocked) result, or which step still needs to run.

Maps directly to the onboarding→audit gap analysis:
- catalog_products  → identity / routability / content base (BLOCKING)
- product_quality_snapshot.content_quality_score → content_richness 25 +
  serving-eligibility gate 30 (BLOCKING)
- product_enrichment → content_richness enrichment_coverage 20 + identity
  title_override (enhancement). Checked element-aware: a row may carry only a
  title_override (lifts identity) yet none of the content elements
  (summary_short/bullet_points/usage_scenarios/audience_tags) enrichment_coverage
  scores — so row-count alone over-credits enrichment.
- products_cache → the live-store source the quality backfill scores from
  (informational; empty means no live store synced yet)
"""
from __future__ import annotations

from typing import Any, Dict, Optional

# Whitelisted dependency tables (table name is interpolated, so it must never
# come from user input).
_DEP_TABLES = (
    "catalog_products",
    "products_cache",
    "product_quality_snapshot",
    "product_enrichment",
)


async def _table_count(
    table: str,
    merchant_id: str,
    *,
    platform: Optional[str] = None,
    where_extra: Optional[str] = None,
) -> int:
    """COUNT(*) for a whitelisted dependency table, scoped to the merchant."""
    if table not in _DEP_TABLES:
        raise ValueError(f"refusing to count non-whitelisted table: {table}")
    from db.database import database

    clauses = ["merchant_id = :merchant_id"]
    values: Dict[str, Any] = {"merchant_id": merchant_id}
    if platform:
        clauses.append("platform = :platform")
        values["platform"] = platform
    if where_extra:
        clauses.append(where_extra)
    sql = f"SELECT COUNT(*) AS n FROM {table} WHERE " + " AND ".join(clauses)
    try:
        row = await database.fetch_one(sql, values)
    except Exception:
        return 0
    return int((dict(row).get("n") if row else 0) or 0)


async def assess_merchant_audit_readiness(
    merchant_id: str,
    platform: str = "shopify",
) -> Dict[str, Any]:
    """Return a structured readiness verdict for a merchant's v3 audit.

    `ready` is True only when the BLOCKING dependencies are present (catalog +
    quality) — i.e. the audit will return real, non-blocked per-SKU scores.
    Enhancement gaps (enrichment) lower the score but don't block.
    """
    counts = {
        "catalog_products": await _table_count("catalog_products", merchant_id, platform=platform),
        "products_cache": await _table_count("products_cache", merchant_id, platform=platform),
        "product_quality_snapshot": await _table_count(
            "product_quality_snapshot", merchant_id, platform=platform,
            where_extra="content_quality_score IS NOT NULL",
        ),
        "product_enrichment": await _table_count("product_enrichment", merchant_id, platform=platform),
        # Element-aware: rows can exist with only a title_override (used by identity)
        # yet carry none of the content elements content_richness.enrichment_coverage
        # actually scores. Count rows that have at least one content element so the
        # probe does not give false "enrichment present" comfort for title-only rows.
        "product_enrichment_with_content": await _table_count(
            "product_enrichment", merchant_id, platform=platform,
            where_extra=(
                "(summary_short IS NOT NULL OR bullet_points IS NOT NULL "
                "OR usage_scenarios IS NOT NULL OR audience_tags IS NOT NULL)"
            ),
        ),
    }

    blocking_gaps = []
    if counts["catalog_products"] == 0:
        blocking_gaps.append(
            "catalog_products empty — run a catalog sync (merchant 'Sync products' "
            "now ingests catalog via WS-A.2, or the catalog-sync job / webhook)."
        )
    if counts["product_quality_snapshot"] == 0:
        blocking_gaps.append(
            "product_quality_snapshot missing content_quality_score — the quality "
            "backfill (auto-enqueued after catalog sync via WS-A.1) hasn't run yet; "
            "gates content_richness 25 + serving-eligibility 30."
        )

    enhancement_gaps = []
    if counts["product_enrichment_with_content"] == 0:
        if counts["product_enrichment"] == 0:
            enhancement_gaps.append(
                "product_enrichment empty — enrichment pipeline hasn't run "
                "(title_override + content elements, ~20 content-richness pts; not blocking)."
            )
        else:
            enhancement_gaps.append(
                f"product_enrichment has {counts['product_enrichment']} row(s) but no content "
                "elements (summary_short/bullet_points/usage_scenarios/audience_tags) — likely "
                "title_override-only; content_richness enrichment_coverage will score 0/20 "
                "until the enrichment pipeline populates content (not blocking)."
            )
    if counts["products_cache"] == 0:
        enhancement_gaps.append(
            "products_cache empty — no live store products synced; quality backfill "
            "will have no candidates until a Shopify sync runs."
        )

    ready = not blocking_gaps
    return {
        "merchant_id": merchant_id,
        "platform": platform,
        "ready": ready,
        "counts": counts,
        "blocking_gaps": blocking_gaps,
        "enhancement_gaps": enhancement_gaps,
        "recommendation": (
            "Audit-ready: scores will be real (enhancement gaps only lower the ceiling)."
            if ready else
            "NOT audit-ready: resolve blocking gaps before trusting the audit — "
            "the report would come back blocked/incomplete."
        ),
    }
