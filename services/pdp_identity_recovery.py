"""Review-gated repair helpers for PDP identity graph coverage.

This module deliberately does not touch external_product_seeds.seed_data or
catalog_products.product_payload. It only repairs identity edges used by PDP
offer fusion: product_group_members, external_product_seeds.attached_product_key,
and catalog_products.pdp_scope.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence

from db.database import database
from services.pdp_scope_classifier import (
    LABEL_SOURCE_ENRICHMENT,
    SCOPE_CANONICAL,
    own_merchant_seller_term_sql,
)


IDENTITY_RECOVERY_SOURCE = "pdp_identity_recovery"
DEFAULT_PROPOSER = "pdp_identity_graph_repair_20260512"
EXTERNAL_SEED_MERCHANT_ID = "external_seed"
EXTERNAL_SEED_PLATFORM = "external_seed"
DEFAULT_SUSPECT_MERCHANT_IDS = tuple(
    value.strip()
    for value in os.getenv("PDP_IDENTITY_RECOVERY_SUSPECT_MERCHANT_IDS", "").split(",")
    if value.strip()
)
IDENTITY_LANE_LIVE_APPROVED = "live_approved"
IDENTITY_LANE_MISSING = "missing"
IDENTITY_LANE_APPROVED_NOT_LIVE = "approved_not_live"
IDENTITY_LANE_REVIEW_REQUIRED = "review_required"
LIVE_IDENTITY_LIFECYCLE_STAGES = {"validated", "published"}


def _clean(value: Any) -> str:
    return str(value or "").strip()


def make_catalog_product_key(merchant_id: str, platform: str, source_product_id: str) -> str:
    """Return the canonical catalog_products.product_key shape."""
    merchant = _clean(merchant_id)
    plat = _clean(platform).lower()
    source_id = _clean(source_product_id)
    if not merchant or not plat or not source_id:
        return ""
    return f"prod::{merchant}::{plat}::{source_id}"


def parse_legacy_attached_product_key(value: Any) -> Optional[Dict[str, str]]:
    """Parse legacy external seed attachment keys.

    Legacy employee tooling wrote keys as merchant|platform|source_product_id.
    Current catalog_products rows use prod::merchant::platform::source_product_id.
    """
    raw = _clean(value)
    if not raw or raw.startswith("prod::"):
        return None
    parts = [part.strip() for part in raw.split("|")]
    if len(parts) != 3 or not all(parts):
        return None
    merchant_id, platform, source_product_id = parts
    return {
        "merchant_id": merchant_id,
        "platform": platform.lower(),
        "source_product_id": source_product_id,
        "catalog_product_key": make_catalog_product_key(merchant_id, platform, source_product_id),
    }


def normalize_title_for_identity(value: Any) -> str:
    text = _clean(value).lower()
    text = re.sub(r"[\u2018\u2019\u201c\u201d]", "'", text)
    text = re.sub(r"[^a-z0-9%+]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def strip_brand_prefix(title: Any, brand: Any) -> str:
    normalized_title = normalize_title_for_identity(title)
    normalized_brand = normalize_title_for_identity(brand)
    if normalized_brand and normalized_title.startswith(f"{normalized_brand} "):
        return normalized_title[len(normalized_brand) + 1 :].strip()
    return normalized_title


def deterministic_product_group_id(product_key: str) -> str:
    digest = hashlib.sha256(_clean(product_key).encode("utf-8")).hexdigest()[:16]
    return f"pg_catalog_{digest}"


def deterministic_ext_identity_group_id(attached_product_key: str) -> str:
    """Stable group id for review-gated ext:* product identities."""
    identity_key = _clean(attached_product_key).lower()
    digest = hashlib.sha256(identity_key.encode("utf-8")).hexdigest()[:16]
    return f"pg_ext_{digest}"


@dataclass(frozen=True)
class IdentityRecoveryProposal:
    action: str
    confidence: float
    reason: str
    product_key: Optional[str] = None
    product_group_id: Optional[str] = None
    merchant_id: Optional[str] = None
    platform: Optional[str] = None
    source_product_id: Optional[str] = None
    is_primary: Optional[bool] = None
    seed_id: Optional[str] = None
    from_attached_product_key: Optional[str] = None
    to_attached_product_key: Optional[str] = None

    @property
    def high_confidence(self) -> bool:
        return self.confidence >= 0.95 and self.reason in {
            "internal_product_missing_group",
            "exact_title_brand_multi_merchant",
            "attached_external_seed_product_group_member",
            "external_seed_catalog_missing_group",
            "ext_identity_attached_key_group_member",
            "legacy_attached_key_exact_product_key",
            "legacy_attached_key_exact_title_same_merchant",
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action": self.action,
            "confidence": self.confidence,
            "reason": self.reason,
            **({"product_key": self.product_key} if self.product_key else {}),
            **({"product_group_id": self.product_group_id} if self.product_group_id else {}),
            **({"merchant_id": self.merchant_id} if self.merchant_id else {}),
            **({"platform": self.platform} if self.platform else {}),
            **({"source_product_id": self.source_product_id} if self.source_product_id else {}),
            **({"is_primary": self.is_primary} if self.is_primary is not None else {}),
            **({"seed_id": self.seed_id} if self.seed_id else {}),
            **(
                {"from_attached_product_key": self.from_attached_product_key}
                if self.from_attached_product_key
                else {}
            ),
            **({"to_attached_product_key": self.to_attached_product_key} if self.to_attached_product_key else {}),
            "high_confidence": self.high_confidence,
        }


def build_singleton_group_proposal(row: Dict[str, Any]) -> Optional[IdentityRecoveryProposal]:
    product_key = _clean(row.get("product_key"))
    merchant_id = _clean(row.get("merchant_id"))
    platform = _clean(row.get("platform")).lower()
    source_product_id = _clean(row.get("source_product_id"))
    offer_count = int(row.get("offer_count") or 0)
    existing_group = _clean(row.get("product_group_id"))
    if not product_key or not merchant_id or not platform or not source_product_id:
        return None
    if existing_group or offer_count <= 0:
        return None
    return IdentityRecoveryProposal(
        action="upsert_product_group_member",
        confidence=1.0,
        reason="internal_product_missing_group",
        product_key=product_key,
        product_group_id=deterministic_product_group_id(product_key),
        merchant_id=merchant_id,
        platform=platform,
        source_product_id=source_product_id,
        is_primary=True,
    )


def build_multi_merchant_group_proposals(rows: Sequence[Dict[str, Any]]) -> List[IdentityRecoveryProposal]:
    """Build conservative same-product group proposals.

    This intentionally requires an exact normalized title and an exact non-empty
    brand match across at least two merchants. Title-only candidates stay out of
    apply and can be reviewed separately.
    """
    products_by_key: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        product_key = _clean(row.get("product_key"))
        merchant_id = _clean(row.get("merchant_id"))
        platform = _clean(row.get("platform")).lower()
        source_product_id = _clean(row.get("source_product_id"))
        title = normalize_title_for_identity(row.get("title"))
        brand = normalize_title_for_identity(row.get("brand"))
        if not (product_key and merchant_id and platform and source_product_id and title and brand):
            continue
        if int(row.get("offer_count") or 0) <= 0:
            continue
        products_by_key[product_key] = {
            **row,
            "product_key": product_key,
            "merchant_id": merchant_id,
            "platform": platform,
            "source_product_id": source_product_id,
            "normalized_title": title,
            "normalized_brand": brand,
            "product_group_id": _clean(row.get("product_group_id")) or None,
            "is_primary": bool(row.get("is_primary")),
        }

    clusters: Dict[tuple[str, str], List[Dict[str, Any]]] = {}
    for row in products_by_key.values():
        key = (row["normalized_brand"], row["normalized_title"])
        clusters.setdefault(key, []).append(row)

    proposals: List[IdentityRecoveryProposal] = []
    for cluster_rows in clusters.values():
        merchant_ids = {_clean(row.get("merchant_id")) for row in cluster_rows}
        if len(merchant_ids) < 2:
            continue

        existing_groups = sorted(
            {_clean(row.get("product_group_id")) for row in cluster_rows if _clean(row.get("product_group_id"))}
        )
        primary_row = next(
            (row for row in cluster_rows if row.get("product_group_id") in existing_groups and row.get("is_primary")),
            None,
        )
        if primary_row is None:
            primary_row = sorted(cluster_rows, key=lambda row: _clean(row.get("product_key")))[0]
        product_group_id = _clean(primary_row.get("product_group_id")) or (
            existing_groups[0]
            if existing_groups
            else deterministic_product_group_id(_clean(primary_row.get("product_key")))
        )
        primary_product_key = _clean(primary_row.get("product_key"))

        for row in sorted(cluster_rows, key=lambda item: _clean(item.get("product_key"))):
            product_key = _clean(row.get("product_key"))
            should_be_primary = product_key == primary_product_key
            if _clean(row.get("product_group_id")) == product_group_id and bool(row.get("is_primary")) == should_be_primary:
                continue
            proposals.append(
                IdentityRecoveryProposal(
                    action="upsert_product_group_member",
                    confidence=0.97,
                    reason="exact_title_brand_multi_merchant",
                    product_key=product_key,
                    product_group_id=product_group_id,
                    merchant_id=_clean(row.get("merchant_id")),
                    platform=_clean(row.get("platform")).lower(),
                    source_product_id=_clean(row.get("source_product_id")),
                    is_primary=should_be_primary,
                )
            )
    return proposals


def build_exact_legacy_attachment_proposal(seed_row: Dict[str, Any]) -> Optional[IdentityRecoveryProposal]:
    attached = _clean(seed_row.get("attached_product_key"))
    parsed = parse_legacy_attached_product_key(attached)
    if not parsed:
        return None
    matched_product_key = _clean(seed_row.get("matched_product_key"))
    if matched_product_key != parsed["catalog_product_key"]:
        return None
    return IdentityRecoveryProposal(
        action="repair_external_seed_attachment",
        confidence=1.0,
        reason="legacy_attached_key_exact_product_key",
        seed_id=_clean(seed_row.get("id")),
        product_key=matched_product_key,
        merchant_id=parsed["merchant_id"],
        platform=parsed["platform"],
        source_product_id=parsed["source_product_id"],
        from_attached_product_key=attached,
        to_attached_product_key=matched_product_key,
    )


def build_stale_title_attachment_proposal(seed_row: Dict[str, Any]) -> Optional[IdentityRecoveryProposal]:
    attached = _clean(seed_row.get("attached_product_key"))
    parsed = parse_legacy_attached_product_key(attached)
    if not parsed:
        return None

    candidate_product_key = _clean(seed_row.get("candidate_product_key"))
    if not candidate_product_key:
        return None

    seed_title = seed_row.get("seed_title") or seed_row.get("title")
    product_title = seed_row.get("candidate_title")
    seed_brand = seed_row.get("seed_brand")
    product_brand = seed_row.get("candidate_brand")
    seed_norm = normalize_title_for_identity(seed_title)
    product_norm = normalize_title_for_identity(product_title)
    seed_without_brand = strip_brand_prefix(seed_title, seed_brand or product_brand)
    product_without_brand = strip_brand_prefix(product_title, product_brand or seed_brand)
    brand_matches = (
        normalize_title_for_identity(seed_brand)
        and normalize_title_for_identity(seed_brand) == normalize_title_for_identity(product_brand)
    )
    title_matches = bool(seed_norm and product_norm and seed_norm == product_norm)
    stripped_title_matches = bool(
        seed_without_brand and product_without_brand and seed_without_brand == product_without_brand
    )
    has_both_brands = bool(normalize_title_for_identity(seed_brand) and normalize_title_for_identity(product_brand))
    title_identity_matches = title_matches and (brand_matches or not has_both_brands)
    if not (title_identity_matches or (brand_matches and stripped_title_matches)):
        return None

    return IdentityRecoveryProposal(
        action="repair_external_seed_attachment",
        confidence=0.98,
        reason="legacy_attached_key_exact_title_same_merchant",
        seed_id=_clean(seed_row.get("id")),
        product_key=candidate_product_key,
        merchant_id=parsed["merchant_id"],
        platform=parsed["platform"],
        source_product_id=_clean(seed_row.get("candidate_source_product_id")),
        from_attached_product_key=attached,
        to_attached_product_key=candidate_product_key,
    )


def build_attached_external_seed_group_member_proposal(
    seed_row: Dict[str, Any],
) -> Optional[IdentityRecoveryProposal]:
    """Attach an already-curated external seed as a seller member.

    external_product_seeds.attached_product_key is the review-gated edge from
    an external referral row to an internal catalog product. PDP offers are
    driven by product_group_members, so the attached seed must also be present
    as an external_seed group member before PDP v2 can show it as another
    merchant offer.
    """
    seed_id = _clean(seed_row.get("id"))
    external_product_id = _clean(seed_row.get("external_product_id"))
    attached_product_key = _clean(seed_row.get("attached_product_key"))
    product_group_id = _clean(seed_row.get("product_group_id"))
    existing_external_group_id = _clean(seed_row.get("existing_external_group_id"))
    if not (seed_id and external_product_id and attached_product_key and product_group_id):
        return None
    if not attached_product_key.startswith("prod::"):
        return None
    if existing_external_group_id == product_group_id:
        return None
    return IdentityRecoveryProposal(
        action="upsert_product_group_member",
        confidence=0.99,
        reason="attached_external_seed_product_group_member",
        seed_id=seed_id,
        product_key=attached_product_key,
        product_group_id=product_group_id,
        merchant_id="external_seed",
        platform="external_seed",
        source_product_id=external_product_id,
        is_primary=False,
    )


def build_external_seed_catalog_group_member_proposal(
    row: Dict[str, Any],
) -> Optional[IdentityRecoveryProposal]:
    """Create a singleton group for mirrored external_seed catalog offers.

    Rows that already participate in a duplicated ext:* identity are handled by
    build_ext_identity_group_member_proposal so the same product does not first
    get a singleton group and then immediately move to the identity group.
    """
    product_key = _clean(row.get("product_key"))
    source_product_id = _clean(row.get("source_product_id"))
    merchant_id = _clean(row.get("merchant_id"))
    platform = _clean(row.get("platform")).lower()
    existing_group = _clean(row.get("product_group_id"))
    ext_identity_cluster_key = _clean(row.get("ext_identity_cluster_key"))
    offer_count = int(row.get("offer_count") or 0)
    if not (
        product_key
        and source_product_id
        and merchant_id == EXTERNAL_SEED_MERCHANT_ID
        and platform == EXTERNAL_SEED_PLATFORM
    ):
        return None
    if offer_count <= 0 or existing_group or ext_identity_cluster_key:
        return None
    return IdentityRecoveryProposal(
        action="upsert_product_group_member",
        confidence=1.0,
        reason="external_seed_catalog_missing_group",
        product_key=product_key,
        product_group_id=deterministic_product_group_id(product_key),
        merchant_id=EXTERNAL_SEED_MERCHANT_ID,
        platform=EXTERNAL_SEED_PLATFORM,
        source_product_id=source_product_id,
        is_primary=True,
    )


def build_ext_identity_group_member_proposal(row: Dict[str, Any]) -> Optional[IdentityRecoveryProposal]:
    """Group external offers that share the same review-gated ext:* identity."""
    attached_product_key = _clean(row.get("attached_product_key"))
    product_key = _clean(row.get("product_key"))
    external_product_id = _clean(row.get("external_product_id") or row.get("source_product_id"))
    existing_group = _clean(row.get("product_group_id"))
    existing_is_primary = bool(row.get("is_primary"))
    cluster_external_products = int(row.get("cluster_external_products") or 0)
    primary_rank = int(row.get("primary_rank") or 0)
    has_offer = bool(row.get("has_offer"))
    if not (
        attached_product_key.startswith("ext:")
        and product_key
        and external_product_id
        and cluster_external_products >= 2
        and has_offer
    ):
        return None
    product_group_id = deterministic_ext_identity_group_id(attached_product_key)
    should_be_primary = primary_rank == 1
    if existing_group == product_group_id and existing_is_primary == should_be_primary:
        return None
    return IdentityRecoveryProposal(
        action="upsert_product_group_member",
        confidence=0.99,
        reason="ext_identity_attached_key_group_member",
        seed_id=_clean(row.get("id")) or None,
        product_key=product_key,
        product_group_id=product_group_id,
        merchant_id=EXTERNAL_SEED_MERCHANT_ID,
        platform=EXTERNAL_SEED_PLATFORM,
        source_product_id=external_product_id,
        is_primary=should_be_primary,
        from_attached_product_key=attached_product_key,
    )


def classify_identity_lane(row: Dict[str, Any]) -> Dict[str, Any]:
    """Classify a catalog row into the identity operating lane.

    Lane semantics:
      - missing: no approved product_group_members edge exists.
      - approved_not_live: approved identity exists, but catalog/live-read
        state is not ready for public serving.
      - review_required: ambiguous multi-domain/title/brand signal; do not
        auto-merge.
      - live_approved: approved identity is aligned with live catalog state.
    """
    if row.get("review_required"):
        return {
            "identity_lane": IDENTITY_LANE_REVIEW_REQUIRED,
            "identity_lane_detail": row.get("review_reason")
            or "identity requires human review before merge",
        }

    product_group_id = _clean(row.get("product_group_id"))
    if not product_group_id:
        return {
            "identity_lane": IDENTITY_LANE_MISSING,
            "identity_lane_detail": "no product_group_members row for catalog product",
        }

    sync_status = _clean(row.get("sync_status")) or "live"
    if sync_status != "live":
        return {
            "identity_lane": IDENTITY_LANE_APPROVED_NOT_LIVE,
            "identity_lane_detail": f"catalog_products.sync_status={sync_status!r}",
        }

    lifecycle_stage = _clean(row.get("pdp_lifecycle_stage"))
    if lifecycle_stage not in LIVE_IDENTITY_LIFECYCLE_STAGES:
        return {
            "identity_lane": IDENTITY_LANE_APPROVED_NOT_LIVE,
            "identity_lane_detail": (
                "identity approved but pdp_lifecycle_stage is not "
                "validated/published"
            ),
        }

    return {
        "identity_lane": IDENTITY_LANE_LIVE_APPROVED,
        "identity_lane_detail": "approved identity is aligned with live catalog state",
    }


def _lane_item(row: Dict[str, Any]) -> Dict[str, Any]:
    lane = classify_identity_lane(row)
    return {
        "identity_lane": lane["identity_lane"],
        "identity_lane_detail": lane["identity_lane_detail"],
        "product_key": row.get("product_key"),
        "content_key": row.get("content_key"),
        "merchant_id": row.get("merchant_id"),
        "platform": row.get("platform"),
        "source_product_id": row.get("source_product_id"),
        "title": row.get("title"),
        "brand": row.get("brand"),
        "canonical_url": row.get("canonical_url"),
        "sync_status": row.get("sync_status"),
        "pdp_lifecycle_stage": row.get("pdp_lifecycle_stage"),
        "pdp_scope": row.get("pdp_scope"),
        "product_group_id": row.get("product_group_id"),
        "resolved_identity_key": row.get("resolved_identity_key"),
        "resolved_identity_source": row.get("resolved_identity_source"),
        "has_positive_offer": row.get("has_positive_offer"),
        "review_cluster_key": row.get("review_cluster_key"),
    }


async def fetch_identity_lane_gap_rows(*, limit: int, offset: int) -> List[Dict[str, Any]]:
    rows = await database.fetch_all(
        """
        SELECT cp.product_key,
               cp.content_key,
               cp.merchant_id,
               cp.platform,
               cp.source_product_id,
               cp.title,
               cp.brand,
               cp.canonical_url,
               cp.sync_status,
               cp.pdp_lifecycle_stage,
               cp.pdp_scope,
               pgm.product_group_id,
               pgm.is_primary,
               EXISTS (
                 SELECT 1
                 FROM catalog_offers co
                 WHERE co.product_key = cp.product_key
                   AND co.suppressed_at IS NULL
                   AND co.list_price > 0
               ) AS has_positive_offer
        FROM catalog_products cp
        LEFT JOIN product_group_members pgm
          ON pgm.merchant_id = cp.merchant_id
         AND pgm.platform = cp.platform
         AND pgm.platform_product_id = cp.source_product_id
        WHERE cp.source_product_id IS NOT NULL
          AND EXISTS (
            SELECT 1
            FROM catalog_offers co
            WHERE co.product_key = cp.product_key
              AND co.suppressed_at IS NULL
              AND co.list_price > 0
          )
          AND (
            pgm.product_group_id IS NULL
            OR cp.sync_status IS DISTINCT FROM 'live'
            OR COALESCE(cp.pdp_lifecycle_stage, '') NOT IN ('validated', 'published')
          )
        ORDER BY
          CASE
            WHEN pgm.product_group_id IS NULL THEN 0
            ELSE 1
          END,
          cp.updated_at DESC NULLS LAST,
          cp.product_key ASC
        LIMIT :limit OFFSET :offset
        """,
        {"limit": int(limit), "offset": int(offset)},
    )
    return [dict(row) for row in rows or []]


async def fetch_identity_review_required_rows(*, limit: int, offset: int) -> List[Dict[str, Any]]:
    rows = await database.fetch_all(
        """
        WITH seed_identity AS (
          SELECT external_product_id, attached_product_key
          FROM (
            SELECT eps.external_product_id,
                   eps.attached_product_key,
                   ROW_NUMBER() OVER (
                     PARTITION BY eps.external_product_id
                     ORDER BY
                       CASE WHEN eps.attached_product_key LIKE 'ext:%' THEN 0 ELSE 1 END,
                       eps.updated_at DESC NULLS LAST,
                       eps.id ASC
                   ) AS rn
            FROM external_product_seeds eps
            WHERE eps.status = 'active'
              AND NULLIF(BTRIM(COALESCE(eps.external_product_id, '')), '') IS NOT NULL
          ) ranked
          WHERE rn = 1
        ),
        base AS (
          SELECT cp.product_key,
                 cp.content_key,
                 cp.merchant_id,
                 cp.platform,
                 cp.source_product_id,
                 cp.title,
                 cp.brand,
                 cp.canonical_url,
                 cp.sync_status,
                 cp.pdp_lifecycle_stage,
                 cp.pdp_scope,
                 lower(regexp_replace(coalesce(cp.canonical_url,''), '^https?://([^/]+).*$','\\1')) AS domain,
                 regexp_replace(lower(btrim(coalesce(cp.title,''))), '[^a-z0-9%+]+', ' ', 'g') AS title_norm,
                 regexp_replace(lower(btrim(coalesce(cp.brand,''))), '[^a-z0-9%+]+', ' ', 'g') AS brand_norm,
                 pgm.product_group_id,
                 CASE
                   WHEN cp.product_key LIKE 'ext:%' THEN cp.product_key
                   WHEN seed_identity.attached_product_key LIKE 'ext:%'
                    AND pgm.product_group_id LIKE 'pg_ext_%'
                     THEN seed_identity.attached_product_key
                   ELSE pgm.product_group_id
                 END AS resolved_identity_key,
                 CASE
                   WHEN cp.product_key LIKE 'ext:%' THEN 'canonical_ext_product_key'
                   WHEN seed_identity.attached_product_key LIKE 'ext:%'
                    AND pgm.product_group_id LIKE 'pg_ext_%'
                     THEN 'ext_identity_group_member'
                   WHEN pgm.product_group_id IS NOT NULL THEN 'product_group_member'
                   ELSE 'missing_group_member'
                 END AS resolved_identity_source,
                 EXISTS (
                   SELECT 1
                   FROM catalog_offers co
                   WHERE co.product_key = cp.product_key
                     AND co.suppressed_at IS NULL
                     AND co.list_price > 0
                 ) AS has_positive_offer
          FROM catalog_products cp
          LEFT JOIN product_group_members pgm
            ON pgm.merchant_id = cp.merchant_id
           AND pgm.platform = cp.platform
           AND pgm.platform_product_id = cp.source_product_id
          LEFT JOIN seed_identity
            ON seed_identity.external_product_id = cp.source_product_id
          WHERE cp.merchant_id = 'external_seed'
            AND cp.source_product_id IS NOT NULL
            AND EXISTS (
              SELECT 1
              FROM catalog_offers co
              WHERE co.product_key = cp.product_key
                AND co.suppressed_at IS NULL
                AND co.list_price > 0
            )
        ),
        clusters AS (
          SELECT title_norm,
                 brand_norm,
                 COUNT(DISTINCT product_key)::int AS products,
                 COUNT(DISTINCT domain)::int AS domains,
                 COUNT(DISTINCT product_group_id) FILTER (WHERE product_group_id IS NOT NULL)::int AS groups,
                 COUNT(DISTINCT resolved_identity_key) FILTER (
                   WHERE resolved_identity_key IS NOT NULL
                 )::int AS resolved_identities,
                 COUNT(*) FILTER (WHERE resolved_identity_key IS NULL)::int AS missing_identity_rows
          FROM base
          WHERE title_norm <> '' AND brand_norm <> ''
          GROUP BY title_norm, brand_norm
          HAVING COUNT(DISTINCT product_key) > 1
             AND COUNT(DISTINCT domain) > 1
             AND (
               COUNT(DISTINCT resolved_identity_key) FILTER (WHERE resolved_identity_key IS NOT NULL) > 1
               OR COUNT(*) FILTER (WHERE resolved_identity_key IS NULL) > 0
             )
        )
        SELECT base.*,
               TRUE AS review_required,
               'exact_title_brand_multi_domain_review_required' AS review_reason,
               base.title_norm || '::' || base.brand_norm AS review_cluster_key
        FROM base
        JOIN clusters
          ON clusters.title_norm = base.title_norm
         AND clusters.brand_norm = base.brand_norm
        ORDER BY base.title_norm ASC, base.domain ASC, base.product_key ASC
        LIMIT :limit OFFSET :offset
        """,
        {"limit": int(limit), "offset": int(offset)},
    )
    return [dict(row) for row in rows or []]


async def build_identity_lane_report(*, limit: int, offset: int) -> Dict[str, Any]:
    gap_rows = await fetch_identity_lane_gap_rows(limit=limit, offset=offset)
    review_rows = await fetch_identity_review_required_rows(limit=limit, offset=offset)
    recovery = await build_recovery_proposals(limit=limit, offset=offset)

    items_by_product_key: Dict[str, Dict[str, Any]] = {}
    for row in review_rows:
        product_key = _clean(row.get("product_key"))
        if product_key:
            items_by_product_key[product_key] = _lane_item(row)
    for row in gap_rows:
        product_key = _clean(row.get("product_key"))
        if product_key and product_key not in items_by_product_key:
            items_by_product_key[product_key] = _lane_item(row)

    items = list(items_by_product_key.values())
    lane_counts: Dict[str, int] = {}
    for item in items:
        lane = str(item.get("identity_lane") or "unknown")
        lane_counts[lane] = lane_counts.get(lane, 0) + 1

    return {
        "dry_run": True,
        "limit": int(limit),
        "offset": int(offset),
        "lane_counts": lane_counts,
        "rows_considered": {
            "identity_gap_rows": len(gap_rows),
            "review_required_rows": len(review_rows),
            "deduped_lane_items": len(items),
        },
        "lanes": items,
        "proposal_preview": {
            "proposal_counts": recovery.get("proposal_counts", {}),
            "proposal_reason_counts": recovery.get("proposal_reason_counts", {}),
            "rejected_count": len(recovery.get("rejected", [])),
        },
    }


async def fetch_internal_group_gap_rows(*, limit: int, offset: int) -> List[Dict[str, Any]]:
    rows = await database.fetch_all(
        """
        SELECT cp.product_key,
               cp.merchant_id,
               cp.platform,
               cp.source_product_id,
               cp.title,
               cp.brand,
               cp.pdp_scope,
               pgm.product_group_id,
               (
                 SELECT COUNT(*)
                 FROM catalog_offers co
                 WHERE co.product_key = cp.product_key
               ) AS offer_count
        FROM catalog_products cp
        LEFT JOIN product_group_members pgm
          ON pgm.merchant_id = cp.merchant_id
         AND pgm.platform = cp.platform
         AND pgm.platform_product_id = cp.source_product_id
        WHERE cp.merchant_id <> 'external_seed'
          AND cp.source_product_id IS NOT NULL
          AND EXISTS (
            SELECT 1 FROM catalog_offers co WHERE co.product_key = cp.product_key
          )
        ORDER BY cp.updated_at DESC NULLS LAST, cp.product_key ASC
        LIMIT :limit OFFSET :offset
        """,
        {"limit": int(limit), "offset": int(offset)},
    )
    return [dict(row) for row in rows or []]


async def fetch_multi_merchant_candidate_rows(*, limit: int, offset: int) -> List[Dict[str, Any]]:
    rows = await database.fetch_all(
        """
        WITH base AS (
          SELECT cp.product_key,
                 cp.merchant_id,
                 cp.platform,
                 cp.source_product_id,
                 cp.title,
                 cp.brand,
                 pgm.product_group_id,
                 pgm.is_primary,
                 (
                   SELECT COUNT(*)
                   FROM catalog_offers co
                   WHERE co.product_key = cp.product_key
                 ) AS offer_count
          FROM catalog_products cp
          LEFT JOIN product_group_members pgm
            ON pgm.merchant_id = cp.merchant_id
           AND pgm.platform = cp.platform
           AND pgm.platform_product_id = cp.source_product_id
          WHERE cp.merchant_id <> 'external_seed'
            AND cp.source_product_id IS NOT NULL
            AND NULLIF(BTRIM(cp.title), '') IS NOT NULL
            AND NULLIF(BTRIM(cp.brand), '') IS NOT NULL
            AND EXISTS (
              SELECT 1 FROM catalog_offers co WHERE co.product_key = cp.product_key
            )
          ORDER BY cp.updated_at DESC NULLS LAST, cp.product_key ASC
          LIMIT :limit OFFSET :offset
        ),
        candidates AS (
          SELECT cp.product_key,
                 cp.merchant_id,
                 cp.platform,
                 cp.source_product_id,
                 cp.title,
                 cp.brand,
                 pgm.product_group_id,
                 pgm.is_primary,
                 (
                   SELECT COUNT(*)
                   FROM catalog_offers co
                   WHERE co.product_key = cp.product_key
                 ) AS offer_count
          FROM catalog_products cp
          LEFT JOIN product_group_members pgm
            ON pgm.merchant_id = cp.merchant_id
           AND pgm.platform = cp.platform
           AND pgm.platform_product_id = cp.source_product_id
          WHERE cp.merchant_id <> 'external_seed'
            AND cp.source_product_id IS NOT NULL
            AND EXISTS (
              SELECT 1 FROM catalog_offers co WHERE co.product_key = cp.product_key
            )
        )
        SELECT DISTINCT candidates.*
        FROM base
        JOIN candidates
          ON candidates.merchant_id <> base.merchant_id
         AND lower(BTRIM(candidates.title)) = lower(BTRIM(base.title))
         AND lower(BTRIM(candidates.brand)) = lower(BTRIM(base.brand))
        UNION
        SELECT DISTINCT base.*
        FROM base
        JOIN candidates
          ON candidates.merchant_id <> base.merchant_id
         AND lower(BTRIM(candidates.title)) = lower(BTRIM(base.title))
         AND lower(BTRIM(candidates.brand)) = lower(BTRIM(base.brand))
        ORDER BY product_key ASC
        """,
        {"limit": int(limit), "offset": int(offset)},
    )
    return [dict(row) for row in rows or []]


async def fetch_legacy_attachment_rows(*, limit: int, offset: int) -> List[Dict[str, Any]]:
    rows = await database.fetch_all(
        """
        WITH legacy AS (
          SELECT eps.id,
                 eps.attached_product_key,
                 eps.title AS seed_title,
                 eps.seed_data->>'title' AS seed_data_title,
                 eps.seed_data->>'brand' AS seed_brand,
                 split_part(eps.attached_product_key, '|', 1) AS legacy_merchant_id,
                 lower(split_part(eps.attached_product_key, '|', 2)) AS legacy_platform,
                 split_part(eps.attached_product_key, '|', 3) AS legacy_source_product_id,
                 (
                   'prod::' ||
                   split_part(eps.attached_product_key, '|', 1) ||
                   '::' ||
                   lower(split_part(eps.attached_product_key, '|', 2)) ||
                   '::' ||
                   split_part(eps.attached_product_key, '|', 3)
                 ) AS normalized_product_key
          FROM external_product_seeds eps
          WHERE eps.status = 'active'
            AND eps.attached_product_key IS NOT NULL
            AND eps.attached_product_key LIKE '%|%|%'
          ORDER BY eps.updated_at DESC NULLS LAST, eps.created_at DESC NULLS LAST, eps.id ASC
          LIMIT :limit OFFSET :offset
        )
        SELECT legacy.*,
               exact_cp.product_key AS matched_product_key,
               candidate_cp.product_key AS candidate_product_key,
               candidate_cp.source_product_id AS candidate_source_product_id,
               candidate_cp.title AS candidate_title,
               candidate_cp.brand AS candidate_brand
        FROM legacy
        LEFT JOIN catalog_products exact_cp
          ON exact_cp.product_key = legacy.normalized_product_key
        LEFT JOIN LATERAL (
          SELECT cp.product_key, cp.source_product_id, cp.title, cp.brand
          FROM catalog_products cp
          WHERE cp.merchant_id = legacy.legacy_merchant_id
            AND cp.platform = legacy.legacy_platform
            AND cp.merchant_id <> 'external_seed'
            AND cp.source_product_id <> legacy.legacy_source_product_id
          ORDER BY
            CASE
              WHEN lower(coalesce(cp.title, '')) = lower(coalesce(legacy.seed_title, legacy.seed_data_title, ''))
              THEN 0 ELSE 1
            END,
            cp.updated_at DESC NULLS LAST
          LIMIT 1
        ) candidate_cp ON exact_cp.product_key IS NULL
        """,
        {"limit": int(limit), "offset": int(offset)},
    )
    return [dict(row) for row in rows or []]


async def fetch_attached_external_seed_group_rows(*, limit: int, offset: int) -> List[Dict[str, Any]]:
    rows = await database.fetch_all(
        """
        WITH attached AS (
          SELECT eps.id,
                 eps.external_product_id,
                 eps.title AS seed_title,
                 eps.attached_product_key,
                 eps.domain,
                 cp.product_key,
                 cp.merchant_id,
                 cp.platform,
                 cp.source_product_id,
                 cp.title AS product_title,
                 cp.brand AS product_brand,
                 pgm.product_group_id,
                 existing.product_group_id AS existing_external_group_id
          FROM external_product_seeds eps
          JOIN catalog_products cp
            ON cp.product_key = eps.attached_product_key
          LEFT JOIN product_group_members pgm
            ON pgm.merchant_id = cp.merchant_id
           AND pgm.platform = cp.platform
           AND pgm.platform_product_id = cp.source_product_id
          LEFT JOIN product_group_members existing
            ON existing.merchant_id = 'external_seed'
           AND existing.platform = 'external_seed'
           AND existing.platform_product_id = eps.external_product_id
          WHERE eps.status = 'active'
            AND NULLIF(BTRIM(COALESCE(eps.external_product_id, '')), '') IS NOT NULL
            AND NULLIF(BTRIM(COALESCE(eps.attached_product_key, '')), '') IS NOT NULL
            AND eps.attached_product_key LIKE 'prod::%'
          ORDER BY eps.updated_at DESC NULLS LAST, eps.created_at DESC NULLS LAST, eps.id ASC
          LIMIT :limit OFFSET :offset
        )
        SELECT *
        FROM attached
        WHERE existing_external_group_id IS DISTINCT FROM product_group_id
        """,
        {"limit": int(limit), "offset": int(offset)},
    )
    return [dict(row) for row in rows or []]


async def fetch_external_seed_catalog_rows(*, limit: int, offset: int) -> List[Dict[str, Any]]:
    rows = await database.fetch_all(
        """
        WITH duplicate_ext_identities AS (
          SELECT attached_product_key
          FROM external_product_seeds
          WHERE status = 'active'
            AND attached_product_key LIKE 'ext:%'
          GROUP BY attached_product_key
          HAVING COUNT(DISTINCT external_product_id) >= 2
        ),
        seed_identity AS (
          SELECT DISTINCT ON (eps.external_product_id)
                 eps.external_product_id,
                 eps.attached_product_key
          FROM external_product_seeds eps
          JOIN duplicate_ext_identities dei
            ON dei.attached_product_key = eps.attached_product_key
          WHERE eps.status = 'active'
            AND NULLIF(BTRIM(COALESCE(eps.external_product_id, '')), '') IS NOT NULL
          ORDER BY eps.external_product_id, eps.updated_at DESC NULLS LAST, eps.id ASC
        )
        SELECT cp.product_key,
               cp.merchant_id,
               cp.platform,
               cp.source_product_id,
               cp.title,
               cp.brand,
               cp.pdp_scope,
               cp.pdp_lifecycle_stage,
               pgm.product_group_id,
               pgm.is_primary,
               seed_identity.attached_product_key AS ext_identity_cluster_key,
               (
                 SELECT COUNT(*)
                 FROM catalog_offers co
                 WHERE co.product_key = cp.product_key
               ) AS offer_count
        FROM catalog_products cp
        LEFT JOIN product_group_members pgm
          ON pgm.merchant_id = cp.merchant_id
         AND pgm.platform = cp.platform
         AND pgm.platform_product_id = cp.source_product_id
        LEFT JOIN seed_identity
          ON seed_identity.external_product_id = cp.source_product_id
        WHERE cp.merchant_id = 'external_seed'
          AND cp.platform = 'external_seed'
          AND cp.source_product_id IS NOT NULL
          AND EXISTS (
            SELECT 1 FROM catalog_offers co WHERE co.product_key = cp.product_key
          )
        ORDER BY cp.updated_at DESC NULLS LAST, cp.product_key ASC
        LIMIT :limit OFFSET :offset
        """,
        {"limit": int(limit), "offset": int(offset)},
    )
    return [dict(row) for row in rows or []]


async def fetch_ext_identity_cluster_rows(*, limit: int, offset: int) -> List[Dict[str, Any]]:
    rows = await database.fetch_all(
        """
        WITH duplicate_ext_identities AS (
          SELECT attached_product_key,
                 COUNT(DISTINCT external_product_id)::int AS cluster_external_products,
                 COUNT(DISTINCT domain)::int AS cluster_domains
          FROM external_product_seeds
          WHERE status = 'active'
            AND attached_product_key LIKE 'ext:%'
            AND NULLIF(BTRIM(COALESCE(external_product_id, '')), '') IS NOT NULL
          GROUP BY attached_product_key
          HAVING COUNT(DISTINCT external_product_id) >= 2
        ),
        ext_rows AS (
          SELECT eps.id,
                 eps.external_product_id,
                 eps.attached_product_key,
                 eps.title AS seed_title,
                 eps.domain,
                 dei.cluster_external_products,
                 dei.cluster_domains,
                 ROW_NUMBER() OVER (
                   PARTITION BY eps.attached_product_key
                   ORDER BY eps.domain ASC NULLS LAST, eps.external_product_id ASC
                 ) AS primary_rank
          FROM external_product_seeds eps
          JOIN duplicate_ext_identities dei
            ON dei.attached_product_key = eps.attached_product_key
          WHERE eps.status = 'active'
          ORDER BY eps.attached_product_key ASC, eps.external_product_id ASC
          LIMIT :limit OFFSET :offset
        )
        SELECT ext_rows.*,
               cp.product_key,
               cp.source_product_id,
               cp.title AS product_title,
               cp.brand AS product_brand,
               pgm.product_group_id,
               COALESCE(pgm.is_primary, FALSE) AS is_primary,
               EXISTS (
                 SELECT 1 FROM catalog_offers co WHERE co.product_key = cp.product_key
               ) AS has_offer
        FROM ext_rows
        LEFT JOIN catalog_products cp
          ON cp.merchant_id = 'external_seed'
         AND cp.platform = 'external_seed'
         AND cp.source_product_id = ext_rows.external_product_id
        LEFT JOIN product_group_members pgm
          ON pgm.merchant_id = 'external_seed'
         AND pgm.platform = 'external_seed'
         AND pgm.platform_product_id = ext_rows.external_product_id
        ORDER BY ext_rows.attached_product_key ASC, ext_rows.external_product_id ASC
        """,
        {"limit": int(limit), "offset": int(offset)},
    )
    return [dict(row) for row in rows or []]


async def build_recovery_proposals(*, limit: int, offset: int) -> Dict[str, Any]:
    group_rows = await fetch_internal_group_gap_rows(limit=limit, offset=offset)
    external_seed_catalog_rows = await fetch_external_seed_catalog_rows(limit=limit, offset=offset)
    ext_identity_rows = await fetch_ext_identity_cluster_rows(limit=limit, offset=offset)
    multi_merchant_rows = await fetch_multi_merchant_candidate_rows(limit=limit, offset=offset)
    legacy_rows = await fetch_legacy_attachment_rows(limit=limit, offset=offset)
    attached_external_seed_rows = await fetch_attached_external_seed_group_rows(limit=limit, offset=offset)

    proposals: List[IdentityRecoveryProposal] = []
    rejected: List[Dict[str, Any]] = []
    multi_merchant_proposals = build_multi_merchant_group_proposals(multi_merchant_rows)
    proposals.extend(multi_merchant_proposals)
    multi_merchant_product_keys = {
        proposal.product_key for proposal in multi_merchant_proposals if proposal.product_key
    }
    for row in group_rows:
        if row.get("product_key") in multi_merchant_product_keys:
            continue
        proposal = build_singleton_group_proposal(row)
        if proposal:
            proposals.append(proposal)
    for row in external_seed_catalog_rows:
        proposal = build_external_seed_catalog_group_member_proposal(row)
        if proposal:
            proposals.append(proposal)
    for row in ext_identity_rows:
        proposal = build_ext_identity_group_member_proposal(row)
        if proposal:
            proposals.append(proposal)
        else:
            reason = "ext_identity_not_high_confidence"
            if not row.get("product_key"):
                reason = "ext_identity_missing_external_seed_catalog_product"
            elif not row.get("has_offer"):
                reason = "ext_identity_missing_catalog_offer"
            rejected.append(
                {
                    "seed_id": row.get("id"),
                    "external_product_id": row.get("external_product_id"),
                    "attached_product_key": row.get("attached_product_key"),
                    "reason": reason,
                }
            )
    for row in legacy_rows:
        proposal = build_exact_legacy_attachment_proposal(row) or build_stale_title_attachment_proposal(row)
        if proposal:
            proposals.append(proposal)
        else:
            rejected.append(
                {
                    "seed_id": row.get("id"),
                    "attached_product_key": row.get("attached_product_key"),
                    "reason": "legacy_attachment_not_high_confidence",
                }
            )
    for row in attached_external_seed_rows:
        proposal = build_attached_external_seed_group_member_proposal(row)
        if proposal:
            proposals.append(proposal)
        else:
            rejected.append(
                {
                    "seed_id": row.get("id"),
                    "external_product_id": row.get("external_product_id"),
                    "attached_product_key": row.get("attached_product_key"),
                    "reason": "attached_external_seed_missing_product_group",
                }
            )

    by_action: Dict[str, int] = {}
    by_reason: Dict[str, int] = {}
    for proposal in proposals:
        by_action[proposal.action] = by_action.get(proposal.action, 0) + 1
        by_reason[proposal.reason] = by_reason.get(proposal.reason, 0) + 1

    return {
        "rows_considered": {
            "internal_group_gap_rows": len(group_rows),
            "external_seed_catalog_rows": len(external_seed_catalog_rows),
            "ext_identity_rows": len(ext_identity_rows),
            "multi_merchant_candidate_rows": len(multi_merchant_rows),
            "legacy_attachment_rows": len(legacy_rows),
            "attached_external_seed_group_rows": len(attached_external_seed_rows),
            "total": (
                len(group_rows)
                + len(external_seed_catalog_rows)
                + len(ext_identity_rows)
                + len(multi_merchant_rows)
                + len(legacy_rows)
                + len(attached_external_seed_rows)
            ),
        },
        "proposal_counts": by_action,
        "proposal_reason_counts": by_reason,
        "proposals": [proposal.to_dict() for proposal in proposals],
        "rejected": rejected,
    }


async def build_identity_graph_audit_report(
    *,
    limit: int,
    offset: int,
    suspect_merchant_ids: Sequence[str] = DEFAULT_SUSPECT_MERCHANT_IDS,
) -> Dict[str, Any]:
    suspect_ids = sorted({_clean(value) for value in suspect_merchant_ids if _clean(value)})
    if not suspect_ids:
        suspect_ids = ["__none__"]
    recovery = await build_recovery_proposals(limit=limit, offset=offset)
    external_seed_coverage = await database.fetch_one(
        """
        SELECT COUNT(*)::int AS external_seed_catalog_products,
               COUNT(*) FILTER (
                 WHERE EXISTS (SELECT 1 FROM catalog_offers co WHERE co.product_key = cp.product_key)
               )::int AS with_offers,
               COUNT(*) FILTER (
                 WHERE EXISTS (
                   SELECT 1
                   FROM product_group_members pgm
                   WHERE pgm.merchant_id = 'external_seed'
                     AND pgm.platform = 'external_seed'
                     AND pgm.platform_product_id = cp.source_product_id
                 )
               )::int AS with_group_member,
               COUNT(*) FILTER (WHERE cp.pdp_scope = 'multi_merchant_canonical')::int AS multi_merchant_scope,
               COUNT(*) FILTER (WHERE cp.pdp_lifecycle_stage IN ('validated','published'))::int AS live_lifecycle
        FROM catalog_products cp
        WHERE cp.merchant_id = 'external_seed'
        """
    )
    prod_attachment_coverage = await database.fetch_one(
        """
        WITH eps AS (
          SELECT id, external_product_id, attached_product_key
          FROM external_product_seeds
          WHERE status = 'active'
            AND attached_product_key LIKE 'prod::%'
        ),
        attached AS (
          SELECT eps.*,
                 cp.product_key AS parent_product_key,
                 pgm.product_group_id AS parent_product_group_id,
                 EXISTS (
                   SELECT 1
                   FROM product_group_members em
                   WHERE em.product_group_id = pgm.product_group_id
                     AND em.merchant_id = 'external_seed'
                     AND em.platform = 'external_seed'
                     AND em.platform_product_id = eps.external_product_id
                 ) AS has_external_seed_member
          FROM eps
          LEFT JOIN catalog_products cp
            ON cp.product_key = eps.attached_product_key
          LEFT JOIN product_group_members pgm
            ON pgm.merchant_id = cp.merchant_id
           AND pgm.platform = cp.platform
           AND pgm.platform_product_id = cp.source_product_id
        )
        SELECT COUNT(*)::int AS prod_key_attached_rows,
               COUNT(*) FILTER (WHERE parent_product_key IS NOT NULL)::int AS parent_catalog_exists,
               COUNT(*) FILTER (WHERE parent_product_group_id IS NOT NULL)::int AS parent_group_exists,
               COUNT(*) FILTER (WHERE has_external_seed_member)::int AS external_member_same_group,
               COUNT(*) FILTER (
                 WHERE parent_product_group_id IS NOT NULL AND NOT has_external_seed_member
               )::int AS missing_external_member
        FROM attached
        """
    )
    ext_identity_coverage = await database.fetch_one(
        """
        WITH eps AS (
          SELECT attached_product_key, external_product_id, domain
          FROM external_product_seeds
          WHERE status = 'active'
            AND attached_product_key LIKE 'ext:%'
        ),
        clusters AS (
          SELECT attached_product_key,
                 COUNT(DISTINCT external_product_id)::int AS external_products,
                 COUNT(DISTINCT domain)::int AS domains
          FROM eps
          GROUP BY attached_product_key
          HAVING COUNT(DISTINCT external_product_id) > 1
        ),
        rows AS (
          SELECT eps.*,
                 clusters.external_products,
                 clusters.domains,
                 cp.product_key,
                 pgm.product_group_id
          FROM eps
          JOIN clusters ON clusters.attached_product_key = eps.attached_product_key
          LEFT JOIN catalog_products cp
            ON cp.merchant_id = 'external_seed'
           AND cp.platform = 'external_seed'
           AND cp.source_product_id = eps.external_product_id
          LEFT JOIN product_group_members pgm
            ON pgm.merchant_id = 'external_seed'
           AND pgm.platform = 'external_seed'
           AND pgm.platform_product_id = eps.external_product_id
        )
        SELECT COUNT(DISTINCT attached_product_key)::int AS duplicate_ext_identity_clusters,
               COUNT(DISTINCT attached_product_key) FILTER (WHERE domains > 1)::int AS multi_domain_clusters,
               COUNT(*)::int AS external_products_in_duplicate_clusters,
               COUNT(*) FILTER (WHERE product_key IS NOT NULL)::int AS catalog_product_exists,
               COUNT(*) FILTER (WHERE product_group_id IS NOT NULL)::int AS has_any_group_member
        FROM rows
        """
    )
    review_only = await database.fetch_one(
        """
        WITH base AS (
          SELECT cp.product_key,
                 cp.source_product_id,
                 lower(regexp_replace(coalesce(cp.canonical_url,''), '^https?://([^/]+).*$','\\1')) AS domain,
                 regexp_replace(lower(btrim(coalesce(cp.title,''))), '[^a-z0-9%+]+', ' ', 'g') AS title_norm,
                 regexp_replace(lower(btrim(coalesce(cp.brand,''))), '[^a-z0-9%+]+', ' ', 'g') AS brand_norm,
                 pgm.product_group_id
          FROM catalog_products cp
          LEFT JOIN product_group_members pgm
            ON pgm.merchant_id = 'external_seed'
           AND pgm.platform = 'external_seed'
           AND pgm.platform_product_id = cp.source_product_id
          WHERE cp.merchant_id = 'external_seed'
            AND EXISTS (SELECT 1 FROM catalog_offers co WHERE co.product_key = cp.product_key)
        ),
        clusters AS (
          SELECT title_norm, brand_norm,
                 COUNT(DISTINCT product_key)::int AS products,
                 COUNT(DISTINCT domain)::int AS domains,
                 COUNT(DISTINCT product_group_id) FILTER (WHERE product_group_id IS NOT NULL)::int AS groups,
                 COUNT(*) FILTER (WHERE product_group_id IS NULL)::int AS missing_group_rows
          FROM base
          WHERE title_norm <> '' AND brand_norm <> ''
          GROUP BY title_norm, brand_norm
          HAVING COUNT(DISTINCT product_key) > 1
             AND COUNT(DISTINCT domain) > 1
        )
        SELECT COUNT(*)::int AS exact_title_brand_multi_domain_clusters,
               COALESCE(SUM(products), 0)::int AS products,
               COUNT(*) FILTER (WHERE groups > 1 OR missing_group_rows > 0)::int AS review_only_unmerged_clusters,
               COALESCE(SUM(products) FILTER (WHERE groups > 1 OR missing_group_rows > 0), 0)::int AS review_only_products
        FROM clusters
        """
    )
    suspect_cache = await database.fetch_one(
        """
        SELECT COUNT(*)::int AS suspect_catalog_products,
               COUNT(*) FILTER (
                 WHERE EXISTS (SELECT 1 FROM catalog_offers co WHERE co.product_key = cp.product_key)
               )::int AS with_offers,
               COUNT(*) FILTER (WHERE cp.pdp_scope = 'multi_merchant_canonical')::int AS multi_merchant_scope,
               COUNT(*) FILTER (WHERE cp.pdp_lifecycle_stage IN ('validated','published'))::int AS live_lifecycle
        FROM catalog_products cp
        WHERE cp.merchant_id = ANY(:suspect_merchant_ids)
        """,
        {"suspect_merchant_ids": suspect_ids},
    )
    merchant_anchor_breakdown = await database.fetch_all(
        """
        WITH store_status AS (
          SELECT merchant_id, platform,
                 bool_or(status = 'active') AS has_active_store,
                 array_agg(DISTINCT status ORDER BY status) AS store_statuses,
                 array_agg(DISTINCT domain ORDER BY domain) AS domains
          FROM merchant_stores
          GROUP BY merchant_id, platform
        )
        SELECT cp.merchant_id,
               cp.platform,
               COALESCE(mo.status, 'NO_MERCHANT') AS merchant_status,
               COALESCE(ss.has_active_store, FALSE) AS has_active_store,
               cp.merchant_id = ANY(:suspect_merchant_ids) AS suspect_cache_cohort,
               ss.store_statuses,
               ss.domains,
               COUNT(DISTINCT cp.product_key)::int AS products,
               COUNT(DISTINCT cp.product_key) FILTER (
                 WHERE EXISTS (SELECT 1 FROM catalog_offers co WHERE co.product_key = cp.product_key)
               )::int AS products_with_offers,
               COUNT(DISTINCT cp.product_key) FILTER (WHERE cp.pdp_scope = 'multi_merchant_canonical')::int AS multi_merchant_scope,
               COUNT(DISTINCT cp.product_key) FILTER (WHERE cp.pdp_lifecycle_stage IN ('validated','published'))::int AS live_lifecycle
        FROM catalog_products cp
        LEFT JOIN merchant_onboarding mo ON mo.merchant_id = cp.merchant_id
        LEFT JOIN store_status ss ON ss.merchant_id = cp.merchant_id AND ss.platform = cp.platform
        WHERE cp.merchant_id <> 'external_seed'
        GROUP BY cp.merchant_id, cp.platform, COALESCE(mo.status, 'NO_MERCHANT'),
                 COALESCE(ss.has_active_store, FALSE), cp.merchant_id = ANY(:suspect_merchant_ids),
                 ss.store_statuses, ss.domains
        ORDER BY products DESC, cp.merchant_id ASC
        """,
        {"suspect_merchant_ids": suspect_ids},
    )
    external = dict(external_seed_coverage or {})
    prod_attachment = dict(prod_attachment_coverage or {})
    ext_identity = dict(ext_identity_coverage or {})
    review = dict(review_only or {})
    suspect = dict(suspect_cache or {})
    root_causes = {
        "missing_external_seed_group_member": max(
            0, int(external.get("with_offers") or 0) - int(external.get("with_group_member") or 0)
        ),
        "stale_or_suspect_merchant_cache": int(suspect.get("suspect_catalog_products") or 0),
        "unpromoted_ext_identity_products": max(
            0,
            int(ext_identity.get("catalog_product_exists") or 0)
            - int(ext_identity.get("has_any_group_member") or 0),
        ),
        "review_only_exact_title_brand_products": int(review.get("review_only_products") or 0),
        "prod_key_attachment_missing_external_member": int(prod_attachment.get("missing_external_member") or 0),
    }
    return {
        "dry_run": True,
        "limit": int(limit),
        "offset": int(offset),
        "suspect_merchant_ids": suspect_ids,
        "root_cause_counts": root_causes,
        "merchant_anchor_breakdown": [dict(row) for row in merchant_anchor_breakdown or []],
        "external_seed_catalog_group_coverage": external,
        "prod_key_attachment_coverage": prod_attachment,
        "ext_identity_duplicate_coverage": ext_identity,
        "review_only_exact_title_brand_multi_domain": review,
        "suspect_cache_cohort": suspect,
        "proposal_preview": recovery,
    }


async def _log_pdp_identity_recovery_audit(
    *,
    product_group_id: str,
    proposer: str,
    action: str,
    details: Dict[str, Any],
) -> None:
    await database.execute(
        """
        INSERT INTO pdp_audit_log (
          id, pdp_id, module_key, action, actor_type, actor_id, details, created_at
        ) VALUES (
          :id, :pdp_id, 'identity', :action, 'system_policy', :actor_id, CAST(:details AS JSONB), NOW()
        )
        """,
        {
            "id": f"audit_{uuid.uuid4().hex}",
            "pdp_id": f"pdp_identity_recovery:{product_group_id}",
            "action": action,
            "actor_id": proposer,
            "details": json.dumps(details, sort_keys=True),
        },
    )


async def _apply_product_group_member(proposal: IdentityRecoveryProposal, *, proposer: str) -> None:
    if not (proposal.product_group_id and proposal.merchant_id and proposal.platform and proposal.source_product_id):
        raise ValueError("INVALID_PRODUCT_GROUP_MEMBER_PROPOSAL")
    await database.execute(
        """
        INSERT INTO product_group_members (
          product_group_id, merchant_id, platform, platform_product_id, is_primary, created_at, updated_at
        ) VALUES (
          :product_group_id, :merchant_id, :platform, :platform_product_id, :is_primary, NOW(), NOW()
        )
        ON CONFLICT (merchant_id, platform, platform_product_id)
        DO UPDATE SET
          product_group_id = EXCLUDED.product_group_id,
          is_primary = EXCLUDED.is_primary,
          updated_at = NOW()
        """,
        {
            "product_group_id": proposal.product_group_id,
            "merchant_id": proposal.merchant_id,
            "platform": proposal.platform,
            "platform_product_id": proposal.source_product_id,
            "is_primary": True if proposal.is_primary is None else bool(proposal.is_primary),
        },
    )
    await _log_pdp_identity_recovery_audit(
        product_group_id=proposal.product_group_id,
        proposer=proposer,
        action="identity_recovery_upsert_product_group_member",
        details=proposal.to_dict(),
    )


async def _apply_external_seed_attachment(proposal: IdentityRecoveryProposal, *, proposer: str) -> None:
    if not (proposal.seed_id and proposal.to_attached_product_key):
        raise ValueError("INVALID_EXTERNAL_SEED_ATTACHMENT_PROPOSAL")
    await database.execute(
        """
        UPDATE external_product_seeds
        SET attached_product_key = :to_attached_product_key,
            updated_at = NOW()
        WHERE id = :seed_id
          AND attached_product_key = :from_attached_product_key
        """,
        {
            "seed_id": proposal.seed_id,
            "from_attached_product_key": proposal.from_attached_product_key,
            "to_attached_product_key": proposal.to_attached_product_key,
        },
    )
    if proposal.product_key:
        group_id = proposal.product_group_id or deterministic_product_group_id(proposal.product_key)
        await _log_pdp_identity_recovery_audit(
            product_group_id=group_id,
            proposer=proposer,
            action="identity_recovery_repair_external_seed_attachment",
            details=proposal.to_dict(),
        )


# The canonical-scope promotion predicate, bound here so
# tests/test_pdp_scope_promotion_agrees_with_classifier.py can import THE
# SHIPPED STRING and diff it against pdp_scope_classifier.classify. An
# earlier version had the test scrape this out of the module source, which
# broke the moment the SQL gained an interpolation — the test then ran a
# predicate containing a literal `{...}` placeholder.
CANONICAL_SCOPE_PREDICATE = """
            -- RULE 1: an agent-authored row is canonical BY INTENT, whatever
            -- today's seller count is. Interpolated, never retyped.
            cp.category_label_source = '{label_source_enrichment}'

            -- RULE 2: seller_count >= 2, spelled EXACTLY as
            -- services/pdp_scope_classifier defines it — "distinct merchants
            -- observed across catalog_offers and external_product_seeds linked
            -- to this product_key (THE ROW'S OWN merchant_id IS INCLUDED)".
            --
            -- ⚠️ THE OWN-MERCHANT TERM IS THE WHOLE POINT. A previous version
            -- dropped the old `EXISTS (one active seed)` branch on the reasoning
            -- that "one seed is not multi-merchant". That is true ONLY inside
            -- the `external_seed` bucket, where the +1 would count a bucket
            -- rather than a seller. For a REAL merchant's row, own merchant (1)
            -- + one active seed domain (1) = 2 = canonical, and the classifier
            -- agrees. Deleting it globally de-promoted 51.9% of the rows this
            -- function is called on — and it is called from
            -- repair_external_seed_attachment, whose source query filters
            -- `cp.merchant_id <> 'external_seed'`, i.e. real merchants only.
            -- So the old branch was not unsound; it was unsound IN THE BUCKET.
            -- The CASE below is what makes that distinction instead of guessing.
            OR (
              {own_merchant_term}
              + (
                SELECT count(DISTINCT co.merchant_id)
                FROM catalog_offers co
                WHERE co.product_key = cp.product_key
                  AND co.merchant_id IS NOT NULL
                  AND co.merchant_id IS DISTINCT FROM cp.merchant_id
              )
              + (
                SELECT count(DISTINCT eps.domain)
                FROM external_product_seeds eps
                WHERE eps.attached_product_key = cp.product_key
                  AND eps.status = 'active'
                  AND eps.domain IS NOT NULL
              )
            ) >= 2

            -- RULE 2 via product groups: a peer at a genuinely DIFFERENT
            -- merchant. Not reducible to the counts above — the peer is another
            -- catalog row, not an offer or a seed on this one.
            OR EXISTS (
              SELECT 1
              FROM product_group_members own
              JOIN product_group_members peer
                ON peer.product_group_id = own.product_group_id
               AND peer.merchant_id <> own.merchant_id
              WHERE own.merchant_id = cp.merchant_id
                AND own.platform = cp.platform
                AND own.platform_product_id = cp.source_product_id
            )

            -- The `pg_ext_*` cohort, inside the bucket. Its members all share
            -- merchant_id='external_seed', so the branch above is unsatisfiable
            -- for them and their seeds hang off an `ext:` key rather than this
            -- row's, so the seed count above sees zero.
            --
            -- ⚠️ GATED ON DISTINCT DOMAIN ACROSS THE CLUSTER, not on differing
            -- platform_product_id. A previous version used the latter and
            -- justified it as "multi-seller by construction, the cluster gate is
            -- COUNT(DISTINCT external_product_id) >= 2". That claim is FALSE:
            -- the gate counts external_product_id, NOT domain, so ONE retailer
            -- listing one product under two SKUs satisfies it. This module's own
            -- health query counts `multi_domain_clusters` separately for exactly
            -- that reason. Domain is the seller signal here — one retailer per
            -- eTLD+1, the same basis ADR-009 keys merch_obs_ on.
            OR EXISTS (
              SELECT 1
              FROM product_group_members own
              WHERE cp.merchant_id = 'external_seed'
                AND own.merchant_id = cp.merchant_id
                AND own.platform = cp.platform
                AND own.platform_product_id = cp.source_product_id
                AND (
                  SELECT count(DISTINCT s2.domain)
                  FROM product_group_members peer
                  JOIN external_product_seeds s2
                    ON s2.external_product_id = peer.platform_product_id
                   AND s2.status = 'active'
                   AND s2.domain IS NOT NULL
                  WHERE peer.product_group_id = own.product_group_id
                ) >= 2
            )
""".format(
    label_source_enrichment=LABEL_SOURCE_ENRICHMENT,
    own_merchant_term=own_merchant_seller_term_sql("cp.merchant_id"),
)


async def _promote_canonical_scopes(product_keys: Sequence[str]) -> int:
    """Promote rows to `multi_merchant_canonical`.

    WHAT CHANGED AND WHY. The old predicate promoted on
    `EXISTS (an active attached seed)` — ONE seed, which is not multi-merchant by
    any reading, and not what `services/pdp_scope_classifier` says. That branch
    is deleted. Everything else is kept.

    The label is not cosmetic: `services/pivot_query_service.py` gives it a +200
    search-rank bonus in THREE places (:1048, :1090, :1476), documented as
    "large enough to dominate every other term" — above exact-SKU (120),
    exact-title (100) and exact-brand (80). A row promoted on one seller
    outranks genuine merchant listings on every matched query.

    THIS PREDICATE IS NOT EQUIVALENT TO `classify()`, and must not be described
    as such. `classify` takes an abstract `seller_count`; this SQL works from the
    signals actually available on a row — offers, attached seeds, product-group
    peers — which do not reduce to that number. An earlier version of this
    function claimed equivalence, and the test written to prove it had to
    restate the SQL's own semantics as its oracle to make the claim come out
    true. What is asserted instead, in
    tests/test_pdp_scope_promotion_agrees_with_classifier.py, are the specific
    properties that matter: named single-seller shapes must NOT promote, named
    multi-seller shapes MUST, and the test drives THIS FUNCTION rather than a
    lifted copy of its SQL.

    ⚠️ TWO CLAIMS AN EARLIER VERSION MADE THAT ARE FALSE — do not reinstate:
      * that `peer.merchant_id <> own.merchant_id` is "structurally
        unsatisfiable" for `external_seed` rows. It is unsatisfiable only when
        EVERY group member is in the bucket; a group mixing a bucket member with
        a real merchant satisfies it.
      * that the bucket's `platform_product_id` branch is merely an unsound
        proxy and can be dropped. See the comment on that branch.
    """
    keys = sorted({_clean(key) for key in product_keys if _clean(key)})
    if not keys:
        return 0
    rows = await database.fetch_all(
        """
        UPDATE catalog_products cp
        SET pdp_scope = :scope,
            pdp_scope_source = :source,
            pdp_scope_set_at = NOW()
        WHERE cp.product_key = ANY(:product_keys)
          AND (
{predicate}
          )
        RETURNING cp.product_key
        """.format(predicate=CANONICAL_SCOPE_PREDICATE),
        {
            "scope": SCOPE_CANONICAL,
            "source": IDENTITY_RECOVERY_SOURCE,
            "product_keys": keys,
        },
    )
    return len(rows or [])


async def apply_recovery_proposals(
    proposals: Iterable[Dict[str, Any] | IdentityRecoveryProposal],
    *,
    proposer: str = DEFAULT_PROPOSER,
) -> Dict[str, Any]:
    normalized: List[IdentityRecoveryProposal] = []
    rejected: List[Dict[str, Any]] = []
    for raw in proposals:
        if isinstance(raw, IdentityRecoveryProposal):
            proposal = raw
        else:
            proposal_args = {
                key: value
                for key, value in raw.items()
                if key in IdentityRecoveryProposal.__dataclass_fields__
            }
            proposal = IdentityRecoveryProposal(**proposal_args)
        if proposal.high_confidence:
            normalized.append(proposal)
        else:
            rejected.append({**proposal.to_dict(), "status": "rejected", "reason": "below_high_confidence_threshold"})

    applied: List[Dict[str, Any]] = []
    touched_product_keys: List[str] = []
    async with database.transaction():
        for proposal in normalized:
            if proposal.action == "upsert_product_group_member":
                await _apply_product_group_member(proposal, proposer=proposer)
            elif proposal.action == "repair_external_seed_attachment":
                await _apply_external_seed_attachment(proposal, proposer=proposer)
            else:
                rejected.append({**proposal.to_dict(), "status": "rejected", "reason": "unknown_action"})
                continue
            if proposal.product_key:
                touched_product_keys.append(proposal.product_key)
            applied.append({**proposal.to_dict(), "status": "merged"})
        promoted_scope_count = await _promote_canonical_scopes(touched_product_keys)

    counts: Dict[str, int] = {}
    reason_counts: Dict[str, int] = {}
    for item in applied:
        counts[item["action"]] = counts.get(item["action"], 0) + 1
        reason = str(item.get("reason") or "")
        if reason:
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
    return {
        "status": "success",
        "proposer": proposer,
        "applied_count": len(applied),
        "rejected_count": len(rejected),
        "promoted_scope_count": promoted_scope_count,
        "applied_counts": counts,
        "applied_reason_counts": reason_counts,
        "applied": applied,
        "rejected": rejected,
    }
