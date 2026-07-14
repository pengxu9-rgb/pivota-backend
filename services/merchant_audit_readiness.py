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

from datetime import datetime, timezone
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


def _parse_iso_utc(value: Any) -> Optional[datetime]:
    """Parse the job table's normalized ISO strings (…Z) back to aware UTC."""
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def _derive_backfill_summary(
    job: Optional[Dict[str, Any]],
    now: datetime,
) -> Optional[Dict[str, Any]]:
    """Shape an active quality-backfill job into the timing/progress the portal
    needs to replace "a few minutes" with a grounded countdown.

    `eta_seconds` is best-effort: only computed once the job is running and has
    touched at least one item (rate = touched / elapsed). It stays None while
    the job is still queued (we can't estimate a rate yet). Pure + clock-injected
    so the derivation is deterministic to test.
    """
    if not job:
        return None
    status = job.get("status")
    total = int(job.get("total_candidates") or 0)
    touched = (
        int(job.get("processed") or 0)
        + int(job.get("skipped") or 0)
        + int(job.get("failed") or 0)
    )
    started = _parse_iso_utc(job.get("started_at"))

    eta_seconds: Optional[int] = None
    if status == "running" and started is not None and touched > 0 and total > touched:
        elapsed = (now - started).total_seconds()
        if elapsed > 0:
            rate = touched / elapsed  # items per second
            if rate > 0:
                eta_seconds = int(min(3600, max(0, (total - touched) / rate)))

    progress_pct: Optional[int] = None
    if total > 0:
        progress_pct = max(0, min(100, int(round(100 * touched / total))))

    return {
        "status": status,
        "requested_at": job.get("requested_at"),
        "started_at": job.get("started_at"),
        "total_candidates": total,
        "processed": touched,
        "progress_pct": progress_pct,
        "eta_seconds": eta_seconds,
    }


async def _get_active_backfill_job(merchant_id: str) -> Optional[Dict[str, Any]]:
    """Lazy import (consistent with _table_count) so the module stays cheap to
    import and the getter is easy to monkeypatch in tests."""
    from db.product_quality_backfill_jobs import get_active_quality_backfill_job

    return await get_active_quality_backfill_job(merchant_id)


async def _summarize_active_backfill(merchant_id: str) -> Optional[Dict[str, Any]]:
    """Best-effort backfill timing for the readiness verdict. Returns None when
    there's no active job or the lookup fails — never blocks the verdict."""
    try:
        job = await _get_active_backfill_job(merchant_id)
    except Exception:
        return None
    return _derive_backfill_summary(job, datetime.now(timezone.utc))


async def _count_substantiated_evidence(merchant_id: str) -> int:
    """Products with >=1 substantiated claim in the cross-vertical evidence store
    (Phase 2a `product_evidence`). Best-effort — the store may be absent or the DB
    non-Postgres; never blocks the verdict. Drives the portal's informational
    'evidence tier' (how much citable evidence the merchant has)."""
    from db.database import database

    try:
        row = await database.fetch_one(
            """
            SELECT COUNT(DISTINCT product_key) AS n
            FROM product_evidence
            WHERE merchant_id = :mid
              AND claims @> '[{"substantiation_status": "substantiated"}]'
            """,
            {"mid": merchant_id},
        )
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

    # Provenance breakdown (merchant-wide, all platforms — NOT platform-scoped
    # like `counts`, which mirrors the dominant-platform audit gate). Lets the
    # portal tell a merchant whose "catalog" is really just historical URL-audit
    # observations to connect a store for a deeper first-party audit, instead of
    # calling those observed rows "synced".
    #   - connected_store_products: rows from a connected commerce integration
    #     (shopify/wix/woocommerce/…) — i.e. NOT the observed/authored platforms.
    #   - observed_referral_products: url_audit + external_seed rows the merchant
    #     never synced (catalog_track='external_referral').
    #   - brand_authored_products: merchant-authored content rows (not a store sync).
    provenance = {
        "connected_store_products": await _table_count(
            "catalog_products", merchant_id,
            where_extra=(
                "platform NOT IN ('url_audit', 'external_seed', 'brand_authored')"
            ),
        ),
        "observed_referral_products": await _table_count(
            "catalog_products", merchant_id,
            where_extra="catalog_track = 'external_referral'",
        ),
        "brand_authored_products": await _table_count(
            "catalog_products", merchant_id,
            where_extra="platform = 'brand_authored'",
        ),
    }
    # Recommend a store sync when the merchant's whole catalog is observed
    # URL-audit/seed rows (from past audits) with no connected store behind it —
    # the readiness audit runs on thin observed data, and a sync unlocks a
    # first-party audit. Advisory only: never flips `ready`.
    recommend_store_sync = (
        provenance["connected_store_products"] == 0
        and provenance["observed_referral_products"] > 0
    )

    # Active quality-backfill timing (status/progress/eta) so the portal's
    # "Preparing your catalog" state shows a grounded countdown instead of a
    # flat "a few minutes". None when no job is in flight; never blocks.
    backfill = await _summarize_active_backfill(merchant_id)
    # Phase 2a evidence tier (informational, never blocks): how many products
    # carry citable (substantiated) merchant evidence in the general store.
    evidence_products = await _count_substantiated_evidence(merchant_id)
    return {
        "merchant_id": merchant_id,
        "platform": platform,
        "ready": ready,
        "counts": counts,
        "provenance": provenance,
        "recommend_store_sync": recommend_store_sync,
        "blocking_gaps": blocking_gaps,
        "enhancement_gaps": enhancement_gaps,
        "backfill": backfill,
        "evidence": {"products_with_substantiated_claims": evidence_products},
        "recommendation": (
            "Audit-ready: scores will be real (enhancement gaps only lower the ceiling)."
            if ready else
            "NOT audit-ready: resolve blocking gaps before trusting the audit — "
            "the report would come back blocked/incomplete."
        ),
    }
