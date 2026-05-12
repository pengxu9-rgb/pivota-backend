"""Review-gated repair helpers for PDP identity graph coverage.

This module deliberately does not touch external_product_seeds.seed_data or
catalog_products.product_payload. It only repairs identity edges used by PDP
offer fusion: product_group_members, external_product_seeds.attached_product_key,
and catalog_products.pdp_scope.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence

from db.database import database
from services.pdp_scope_classifier import SCOPE_CANONICAL


IDENTITY_RECOVERY_SOURCE = "pdp_identity_recovery"
DEFAULT_PROPOSER = "pdp_identity_recovery_20260511"


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


async def build_recovery_proposals(*, limit: int, offset: int) -> Dict[str, Any]:
    group_rows = await fetch_internal_group_gap_rows(limit=limit, offset=offset)
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
    for proposal in proposals:
        by_action[proposal.action] = by_action.get(proposal.action, 0) + 1

    return {
        "rows_considered": {
            "internal_group_gap_rows": len(group_rows),
            "multi_merchant_candidate_rows": len(multi_merchant_rows),
            "legacy_attachment_rows": len(legacy_rows),
            "attached_external_seed_group_rows": len(attached_external_seed_rows),
            "total": (
                len(group_rows)
                + len(multi_merchant_rows)
                + len(legacy_rows)
                + len(attached_external_seed_rows)
            ),
        },
        "proposal_counts": by_action,
        "proposals": [proposal.to_dict() for proposal in proposals],
        "rejected": rejected,
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


async def _promote_canonical_scopes(product_keys: Sequence[str]) -> int:
    keys = sorted({_clean(key) for key in product_keys if _clean(key)})
    if not keys:
        return 0
    result = await database.execute(
        """
        UPDATE catalog_products cp
        SET pdp_scope = :scope,
            pdp_scope_source = :source,
            pdp_scope_set_at = NOW()
        WHERE cp.product_key = ANY(:product_keys)
          AND (
            EXISTS (
              SELECT 1
              FROM external_product_seeds eps
              WHERE eps.status = 'active'
                AND eps.attached_product_key = cp.product_key
            )
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
          )
        """,
        {
            "scope": SCOPE_CANONICAL,
            "source": IDENTITY_RECOVERY_SOURCE,
            "product_keys": keys,
        },
    )
    match = re.search(r"\bUPDATE\s+(\d+)", str(result or ""))
    return int(match.group(1)) if match else len(keys)


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
    for item in applied:
        counts[item["action"]] = counts.get(item["action"], 0) + 1
    return {
        "status": "success",
        "proposer": proposer,
        "applied_count": len(applied),
        "rejected_count": len(rejected),
        "promoted_scope_count": promoted_scope_count,
        "applied_counts": counts,
        "applied": applied,
        "rejected": rejected,
    }
