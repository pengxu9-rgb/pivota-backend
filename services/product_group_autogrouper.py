"""Auto-populate product_group_members from content_key clusters.

Stage 2b-i of the PDP architecture roadmap (plans/rosy-mixing-bengio.md).
Stage 1 introduced content_key — content-derived product identity that
collides for the same physical product across paths/merchants. This
service consumes those clusters and creates the multi-merchant grouping
that agent UI uses to render "this product is available from N sellers"
cards.

Background — why this matters:
  - product_group_members is the only mechanism agent UI consults for
    multi-seller cards (services/pdp_governance_service.py:704-778
    _resolve_product_group).
  - Pre-2b, it was 100% operator-curated. With many merchants onboarding,
    curation backlog blocks the multi-seller experience entirely.
  - Stage 1 backfill (PR #475) populated content_key on 4895/4896 rows
    (99.98%). Top duplicate clusters: MOYU 26+18+16 (Path A variant-as-
    product), Tom Ford 43+40+20+20+20 (Path B shade-as-product). Both
    are real catalog rows, not stale caches — they need GROUPING, not
    deduping. That's exactly what this service produces.

What this service does NOT do:
  - Touch catalog_products. Identity stays where it is; only the
    grouping layer changes.
  - Touch seed_data / product_payload. Same reason.
  - Pick which content_key wins in a cross-merchant title clash. That's
    Stage 2b-ii (variant_attributes extraction) territory; today we
    accept the content_key collision as the grouping signal.

Group ID convention:
  product_group_id = "pg_" + content_key.removeprefix("ck_")
  e.g. ck_32de31827aded89c8d0339895b6a2786 → pg_32de31827aded89c8d0339895b6a2786

Deterministic — re-running the autogrouper is idempotent (same input
yields same group IDs and same primary selection).

Primary-row selection (within a cluster):
  1. Rank by pdp_lifecycle_stage: published(4) > validated(3) >
     candidate(2) > draft(1) > NULL/other(0). Most-developed PDP wins.
  2. Tie-break by pivota_signature_minted_at (earlier = more-stable
     canonical URL).
  3. Tie-break by product_key (deterministic lexicographic).

This matches the curated-operator behavior in pdp_governance_service.py
plus a deterministic tiebreak so re-runs don't churn the primary flag.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from db.database import database

logger = logging.getLogger(__name__)


_LIFECYCLE_RANK = {
    "published": 4,
    "validated": 3,
    "candidate": 2,
    "draft": 1,
}


@dataclass
class ClusterRow:
    """One catalog_products row inside a content_key cluster."""

    product_key: str
    merchant_id: str
    platform: str
    source_product_id: str
    pdp_lifecycle_stage: Optional[str]
    pivota_signature_minted_at: Optional[Any]


@dataclass
class ClusterOutcome:
    content_key: str
    product_group_id: str
    member_count: int
    primary_product_key: str
    members_upserted: int = 0
    primary_flag_writes: int = 0
    dry_run: bool = True


@dataclass
class AutogroupReport:
    clusters_considered: int = 0
    clusters_grouped: int = 0
    members_upserted_total: int = 0
    per_cluster: List[ClusterOutcome] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Group ID derivation
# ---------------------------------------------------------------------------


def derive_product_group_id(content_key: str) -> str:
    """ck_<32hex> → pg_<32hex>. Deterministic, idempotent on re-runs.

    We intentionally do NOT use a separate sha256 derivation here — the
    content_key already encodes brand+title+gtin identity. Reusing its
    hex tail keeps it trivially debuggable (operator can grep both
    tables on the same suffix)."""
    if not isinstance(content_key, str) or not content_key.startswith("ck_"):
        raise ValueError(f"invalid content_key: {content_key!r}")
    return "pg_" + content_key[len("ck_"):]


# ---------------------------------------------------------------------------
# Singleton product-group minting (ADR-009 ratified decision 1 — no-fallback)
# ---------------------------------------------------------------------------
#
# ADR-009 "Resolved decisions" 1: the offer's product key is `product_group_id`
# UNCONDITIONALLY — the originally proposed "pg when present, content_key
# fallback" was rejected as the crutch pattern. Instead the mainline is made
# total: every product carries a pg. Where a product is not part of a
# multi-member cluster (this autogrouper only groups clusters of size >= 2,
# see `autogroup_clusters` below) it gets a deterministic SINGLETON group,
# derived from its own `content_key` — the SAME derivation the multi-member
# path uses (`derive_product_group_id`). `content_key` is a DERIVATION INPUT,
# never a runtime alternative.
#
# NON-MERGE PROPERTY (IDENTITY_REFERENCE §2 "mis-merge is worse than
# fragmentation", brand_verified_graduation.py:71):
#   - singleton pg = pg_ + hex(content_key). content_key IS the cross-merchant
#     physical-product identity (IDENTITY_REFERENCE §2 / Trap T6), so two rows
#     that share a content_key legitimately share the singleton pg — that is
#     CORRECT grouping of ONE physical product, not a merge of DISTINCT
#     products. Two DIFFERENT content_keys always map to DIFFERENT pgs.
#   - the singleton pg is byte-identical to what `autogroup_clusters` would
#     mint for the same content_key, so a singleton that later gains a second
#     listing grows seamlessly into a real multi-member group under the SAME
#     pg (no re-key, no fork).
#   - the membership write is ON CONFLICT DO NOTHING, so a row already claimed
#     by a real/curated group is never overwritten with a singleton (we never
#     re-subject an existing grouping — no auto-merge, no mis-merge).
#
# CROSS-MERCHANT (not per-merchant): the singleton is keyed on `content_key`
# ALONE — matching how the multi-member path groups (`_build_cluster_query`
# groups BY content_key with NO merchant filter, and `derive_product_group_id`
# takes only content_key). ADR-009 D1: the PRODUCT layer is cross-merchant,
# seller-free by construction; the seller lives on the OFFER, never the group.


def make_singleton_product_group_id(content_key: str) -> str:
    """Deterministic singleton product_group_id for a lone content identity.

    Same content_key → same pg forever (idempotent). Requires a non-empty
    `ck_`-prefixed content_key: a singleton is a group of exactly ONE content
    identity, so minting one from an absent content_key would be inventing a
    pg from nothing — banned (ADR-009 decision 1: `content_key` is a derivation
    INPUT, never a runtime alternative; store-less rows stay pg-NULL and are
    handled honestly downstream, not force-minted).

    Reuses `derive_product_group_id` so the singleton namespace is IDENTICAL to
    the multi-member namespace (`pg_<hex>`) — a singleton is just an autogroup
    of size 1 (see the NON-MERGE PROPERTY note above)."""
    if not isinstance(content_key, str) or not content_key.strip():
        raise ValueError(
            "cannot mint a singleton product_group_id without a content_key "
            "(ADR-009 decision 1: never mint a pg from nothing)"
        )
    return derive_product_group_id(content_key.strip())


_UPSERT_SINGLETON_MEMBER_SQL = """
    INSERT INTO product_group_members (
      product_group_id, merchant_id, platform, platform_product_id,
      is_primary, updated_at
    ) VALUES (
      :product_group_id, :merchant_id, :platform, :platform_product_id,
      TRUE, NOW()
    )
    ON CONFLICT (merchant_id, platform, platform_product_id)
    DO NOTHING
"""


async def ensure_singleton_group_membership(
    *,
    merchant_id: str,
    platform: str,
    source_product_id: str,
    content_key: Optional[str],
    db: Any = None,
) -> Optional[str]:
    """Materialize the singleton pg for one catalog listing at ingestion time.

    ADR-009 decision 1 (no-fallback): so the offer path can key on pg with ZERO
    branching, every product must carry a pg BEFORE it is served — computing pg
    at offer time would itself be the banned runtime fallback. This stamps the
    singleton membership row (the `product_group_members` table that
    `offers.resolve` reads, agent_shop_gateway.py) when the listing is written.

    Idempotent + non-destructive:
      - `ON CONFLICT DO NOTHING`: a row already claimed by a real/curated group
        (autogrouper cluster, variant promoter, manual governance) is left
        untouched — a singleton NEVER overwrites a real group (no auto-merge).
      - deterministic pg (`make_singleton_product_group_id`), so a re-run and
        the autogroup path converge on the same value.

    Honest absence: when `content_key` is NULL/blank the listing has no
    canonical content identity yet (store-less / thin row). We do NOT invent a
    pg — we leave the product pg-NULL and log observably. Downstream
    (`offers.resolve`) treats a pg-NULL product as `no_canonical_identity`, not
    a silent content_key substitution.

    Returns the singleton pg when minted, else None.
    """
    ck = (content_key or "").strip()
    if not ck:
        logger.info(
            "pg_singleton.skip.no_content_key",
            extra={
                "event": "pg_singleton.skip.no_content_key",
                "merchant_id": merchant_id,
                "platform": platform,
                "source_product_id": source_product_id,
            },
        )
        return None

    pg = make_singleton_product_group_id(ck)
    conn = db if db is not None else database
    await conn.execute(
        _UPSERT_SINGLETON_MEMBER_SQL,
        {
            "product_group_id": pg,
            "merchant_id": merchant_id,
            "platform": platform,
            "platform_product_id": source_product_id,
        },
    )
    logger.debug(
        "pg_singleton.minted",
        extra={
            "event": "pg_singleton.minted",
            "product_group_id": pg,
            "merchant_id": merchant_id,
            "platform": platform,
            "source_product_id": source_product_id,
        },
    )
    return pg


def _lifecycle_rank(stage: Optional[str]) -> int:
    if not isinstance(stage, str):
        return 0
    return _LIFECYCLE_RANK.get(stage.lower(), 0)


def pick_primary(rows: List[ClusterRow]) -> ClusterRow:
    """Pick the canonical row for a content_key cluster. See module
    docstring for the policy. Stable: re-running on the same input
    yields the same primary."""
    if not rows:
        raise ValueError("cannot pick primary from empty cluster")

    def sort_key(r: ClusterRow):
        # Higher is better; invert so sorted() with reverse=True picks
        # the most-developed-then-earliest-signed-then-lowest-key.
        lifecycle = _lifecycle_rank(r.pdp_lifecycle_stage)
        # pivota_signature_minted_at: earlier = better → flip with
        # min() semantics; we'll handle in the sort below.
        return (lifecycle, r.pivota_signature_minted_at, r.product_key)

    # Sort by lifecycle DESC, signature_minted_at ASC (NULLS last),
    # product_key ASC. The cleanest way: precompute a tuple that
    # already encodes the desired ordering.
    def order_tuple(r: ClusterRow):
        # 1. lifecycle: HIGHER rank first → negate so ASC sort picks it.
        # 2. signature_minted_at: EARLIER first, NULL LAST.
        # 3. product_key: LOWER first.
        sig = r.pivota_signature_minted_at
        return (
            -_lifecycle_rank(r.pdp_lifecycle_stage),  # negate for DESC
            (sig is None, sig),                       # NULL last, then ASC
            r.product_key,                            # lexicographic ASC
        )

    return sorted(rows, key=order_tuple)[0]


# ---------------------------------------------------------------------------
# Core query: find clusters
# ---------------------------------------------------------------------------


def _build_cluster_query(*, content_key: Optional[str], merchant_id: Optional[str], limit: int) -> Tuple[str, Dict[str, Any]]:
    """Returns (sql, params) for the cluster fetch. We pull every row
    of every cluster that has count >= 2 — cheap because content_key
    has a partial index (mig 083 idx_catalog_products_content_key).

    Filter shapes:
      - content_key=X: single cluster, every row of it
      - merchant_id=M: every cluster where M has at least 2 rows
      - both NULL: every cluster with count >= 2 across the catalog
    """
    where_clauses = ["cp.content_key IS NOT NULL"]
    params: Dict[str, Any] = {}
    cluster_filter = ""

    if content_key is not None:
        where_clauses.append("cp.content_key = :content_key")
        params["content_key"] = content_key
        # Single-cluster mode: no need for the HAVING filter
    else:
        # Multi-cluster mode: only return clusters with count >= 2.
        # We filter in a CTE so the row-level WHERE still gets the
        # right rows after the cluster filter is applied.
        if merchant_id is not None:
            cluster_filter = (
                "WITH eligible AS ("
                "  SELECT content_key FROM catalog_products"
                "  WHERE content_key IS NOT NULL AND merchant_id = :merchant_id"
                "  GROUP BY content_key HAVING count(*) >= 2"
                f"  ORDER BY content_key LIMIT :limit_clusters"
                ") "
            )
            params["merchant_id"] = merchant_id
            params["limit_clusters"] = int(limit)
            where_clauses.append("cp.content_key IN (SELECT content_key FROM eligible)")
        else:
            cluster_filter = (
                "WITH eligible AS ("
                "  SELECT content_key FROM catalog_products"
                "  WHERE content_key IS NOT NULL"
                "  GROUP BY content_key HAVING count(*) >= 2"
                f"  ORDER BY content_key LIMIT :limit_clusters"
                ") "
            )
            params["limit_clusters"] = int(limit)
            where_clauses.append("cp.content_key IN (SELECT content_key FROM eligible)")

    where_sql = " AND ".join(where_clauses)
    sql = f"""
        {cluster_filter}
        SELECT
          cp.content_key,
          cp.product_key,
          cp.merchant_id,
          cp.platform,
          cp.source_product_id,
          cp.pdp_lifecycle_stage,
          cp.pivota_signature_minted_at
        FROM catalog_products cp
        WHERE {where_sql}
        ORDER BY cp.content_key, cp.product_key
    """
    return sql, params


async def _fetch_clusters(*, content_key: Optional[str], merchant_id: Optional[str], limit: int) -> Dict[str, List[ClusterRow]]:
    sql, params = _build_cluster_query(
        content_key=content_key, merchant_id=merchant_id, limit=limit,
    )
    rows = await database.fetch_all(sql, params)
    clusters: Dict[str, List[ClusterRow]] = {}
    for row in rows or []:
        d = dict(row)
        ck = d["content_key"]
        clusters.setdefault(ck, []).append(ClusterRow(
            product_key=d["product_key"],
            merchant_id=d["merchant_id"],
            platform=d["platform"],
            source_product_id=d["source_product_id"],
            pdp_lifecycle_stage=d.get("pdp_lifecycle_stage"),
            pivota_signature_minted_at=d.get("pivota_signature_minted_at"),
        ))
    return clusters


# ---------------------------------------------------------------------------
# Upsert + primary management
# ---------------------------------------------------------------------------

_UPSERT_MEMBER_SQL = """
    INSERT INTO product_group_members (
      product_group_id, merchant_id, platform, platform_product_id,
      is_primary, updated_at
    ) VALUES (
      :product_group_id, :merchant_id, :platform, :platform_product_id,
      :is_primary, NOW()
    )
    ON CONFLICT (merchant_id, platform, platform_product_id)
    DO UPDATE SET
      product_group_id = EXCLUDED.product_group_id,
      is_primary = EXCLUDED.is_primary,
      updated_at = NOW()
"""


_CLEAR_OTHER_PRIMARIES_SQL = """
    UPDATE product_group_members
    SET is_primary = FALSE,
        updated_at = NOW()
    WHERE product_group_id = :product_group_id
      AND NOT (merchant_id = :primary_merchant_id
               AND platform = :primary_platform
               AND platform_product_id = :primary_platform_product_id)
      AND is_primary = TRUE
"""


async def _apply_cluster(*, content_key: str, rows: List[ClusterRow]) -> ClusterOutcome:
    """Apply one cluster: upsert each member with the right is_primary
    flag, then make sure no other row in the same group has lingering
    is_primary=TRUE (defensive — handles primary migration on re-run).

    Wrapped in a transaction so partial application can't leave a
    cluster with zero or multiple primaries simultaneously visible to
    readers."""
    primary = pick_primary(rows)
    group_id = derive_product_group_id(content_key)

    members_upserted = 0
    async with database.transaction():
        for r in rows:
            is_primary = (
                r.merchant_id == primary.merchant_id
                and r.platform == primary.platform
                and r.source_product_id == primary.source_product_id
            )
            await database.execute(
                _UPSERT_MEMBER_SQL,
                {
                    "product_group_id": group_id,
                    "merchant_id": r.merchant_id,
                    "platform": r.platform,
                    "platform_product_id": r.source_product_id,
                    "is_primary": is_primary,
                },
            )
            members_upserted += 1

        # Clear any lingering primaries elsewhere in the group (handles
        # the case where primary selection changed between runs).
        await database.execute(
            _CLEAR_OTHER_PRIMARIES_SQL,
            {
                "product_group_id": group_id,
                "primary_merchant_id": primary.merchant_id,
                "primary_platform": primary.platform,
                "primary_platform_product_id": primary.source_product_id,
            },
        )

    return ClusterOutcome(
        content_key=content_key,
        product_group_id=group_id,
        member_count=len(rows),
        primary_product_key=primary.product_key,
        members_upserted=members_upserted,
        primary_flag_writes=1,
        dry_run=False,
    )


def _dry_run_outcome(content_key: str, rows: List[ClusterRow]) -> ClusterOutcome:
    primary = pick_primary(rows)
    return ClusterOutcome(
        content_key=content_key,
        product_group_id=derive_product_group_id(content_key),
        member_count=len(rows),
        primary_product_key=primary.product_key,
        members_upserted=0,
        primary_flag_writes=0,
        dry_run=True,
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def autogroup_clusters(
    *,
    content_key: Optional[str] = None,
    merchant_id: Optional[str] = None,
    apply: bool = False,
    limit: int = 100,
) -> AutogroupReport:
    """Iterate eligible content_key clusters and group their rows under
    a deterministic product_group_id. See module docstring for policy.

    Args:
      content_key: single cluster mode (overrides cluster discovery)
      merchant_id: scope to clusters where this merchant has ≥2 rows.
                   Use to limit blast radius on a first run.
      apply: False (default) does dry-run; True writes to product_group_members.
      limit: cap on number of clusters processed per run. Default 100.
             Use --limit 0 for "all" (caller-side argparse should respect)."""
    report = AutogroupReport()

    clusters = await _fetch_clusters(
        content_key=content_key, merchant_id=merchant_id, limit=limit,
    )

    for ck, rows in clusters.items():
        report.clusters_considered += 1
        # Defensive: if the eligible CTE missed a singleton (shouldn't),
        # skip it.
        if len(rows) < 2:
            continue
        if apply:
            outcome = await _apply_cluster(content_key=ck, rows=rows)
            report.members_upserted_total += outcome.members_upserted
        else:
            outcome = _dry_run_outcome(ck, rows)
        report.clusters_grouped += 1
        report.per_cluster.append(outcome)

    return report
