#!/usr/bin/env python3
"""Repair Shopify catalog_offers that point at missing catalog_skus.

This is the PR-2 repair lane after the writer lockdown:

1. Read unsuppressed shopify_products_sync offers whose sku_key has no
   catalog_skus row.
2. Insert the missing catalog_skus row only when the source product and
   variant identity can be resolved deterministically.
3. Soft-suppress every unresolved orphan offer with a reason; never delete,
   never infer price, and never promote serving eligibility.

Dry-run:
  python3 scripts/repair_orphan_shopify_offers.py --limit 500

Apply:
  python3 scripts/repair_orphan_shopify_offers.py \
    --apply --confirm REPAIR_ORPHAN_SHOPIFY_OFFERS --limit 500
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import sys
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, AsyncIterator, Dict, Iterable, List, Mapping, Optional, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from db.database import database  # noqa: E402
from services.catalog_offer_writer_guard import (  # noqa: E402
    WriterAuditAccumulator,
    write_writer_audit_log,
)


CONFIRM_TOKEN = "REPAIR_ORPHAN_SHOPIFY_OFFERS"
REPAIR_WRITER_NAME = "orphan_shopify_offer_repair_v1"
SOURCE_SYSTEM = "shopify_products_sync"

CATALOG_PRODUCT_MISSING = "catalog_product_missing"
PRODUCT_IDENTITY_MISSING = "product_identity_missing"
SOURCE_VARIANT_ID_MISSING = "source_variant_id_missing"
SOURCE_VARIANT_ID_AMBIGUOUS = "source_variant_id_ambiguous"
SOURCE_VARIANT_ID_CONFLICT = "source_variant_id_conflict"
POSITIVE_LIST_PRICE_MISSING = "positive_list_price_missing"
SKU_IDENTITY_CONFLICT = "sku_identity_conflict"
DUPLICATE_SOURCE_IDENTITY_IN_BATCH = "duplicate_source_identity_in_batch"
SKU_TITLE_MISSING = "sku_title_missing"


ORPHAN_SHOPIFY_OFFERS_QUERY = """
SELECT
  o.offer_id,
  o.sku_key,
  o.product_key,
  o.merchant_id,
  o.catalog_track,
  o.truth_tier,
  o.readiness_tier AS offer_readiness_tier,
  o.currency AS offer_currency,
  o.list_price,
  o.merchant_effective_price,
  o.offer_payload,
  o.source_ref,
  cp.platform AS product_platform,
  cp.source_product_id AS catalog_source_product_id,
  cp.title AS product_title,
  cp.image_url AS product_image_url,
  cp.product_payload,
  cp.readiness_tier AS product_readiness_tier,
  pc.product_data AS cache_product_data
FROM catalog_offers o
LEFT JOIN catalog_skus s
  ON s.sku_key = o.sku_key
LEFT JOIN catalog_products cp
  ON cp.product_key = o.product_key
LEFT JOIN products_cache pc
  ON pc.merchant_id = o.merchant_id
 AND pc.platform = cp.platform
 AND pc.platform_product_id = cp.source_product_id
WHERE o.source_system = 'shopify_products_sync'
  AND o.suppressed_at IS NULL
  AND s.sku_key IS NULL
ORDER BY o.updated_at DESC NULLS LAST, o.offer_id ASC
{limit_clause}
"""


EXISTING_SOURCE_IDENTITY_SKU_QUERY = """
SELECT sku_key
FROM catalog_skus
WHERE merchant_id = :merchant_id
  AND platform = :platform
  AND product_key = :product_key
  AND source_variant_id = :source_variant_id
LIMIT 1
"""


INSERT_REPAIR_SKU_SQL = """
INSERT INTO catalog_skus (
  sku_key,
  product_key,
  merchant_id,
  platform,
  source_product_id,
  source_variant_id,
  sku,
  barcode,
  title,
  currency,
  image_url,
  visible_attributes,
  visible_option_labels,
  ingredient_ids,
  sku_payload,
  readiness_tier,
  created_at,
  updated_at
) VALUES (
  :sku_key,
  :product_key,
  :merchant_id,
  :platform,
  :source_product_id,
  :source_variant_id,
  :sku,
  :barcode,
  :title,
  :currency,
  :image_url,
  CAST(:visible_attributes AS jsonb),
  CAST(:visible_option_labels AS jsonb),
  CAST(:ingredient_ids AS jsonb),
  CAST(:sku_payload AS jsonb),
  :readiness_tier,
  NOW(),
  NOW()
)
ON CONFLICT (sku_key) DO NOTHING
"""


SUPPRESS_ORPHAN_OFFER_SQL = """
UPDATE catalog_offers
SET
  suppression_reason = :suppression_reason,
  suppressed_at = NOW(),
  updated_at = NOW()
WHERE offer_id = :offer_id
  AND source_system = 'shopify_products_sync'
  AND suppressed_at IS NULL
"""


POSTCHECK_SQL = """
SELECT
  count(*) FILTER (
    WHERE s.sku_key IS NULL
      AND o.suppressed_at IS NULL
  )::int AS unsuppressed_orphan_offers,
  count(*) FILTER (
    WHERE o.suppressed_at IS NULL
      AND (o.list_price IS NULL OR o.list_price <= 0)
  )::int AS unsuppressed_zero_or_missing_price_offers,
  count(*) FILTER (
    WHERE o.suppressed_at IS NOT NULL
  )::int AS suppressed_shopify_offers
FROM catalog_offers o
LEFT JOIN catalog_skus s
  ON s.sku_key = o.sku_key
WHERE o.source_system = 'shopify_products_sync'
"""


@dataclass
class RepairPlan:
    action: str
    reason: Optional[str]
    offer_id: str
    sku_key: str
    source_identity: Optional[Tuple[str, str, str, str]]
    sku_values: Optional[Dict[str, Any]]
    sample: Dict[str, Any]


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _json_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _json_list(value: Any) -> List[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
        except Exception:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _first_non_empty(*values: Any) -> Optional[str]:
    for value in values:
        cleaned = _clean(value)
        if cleaned:
            return cleaned
    return None


def _positive_decimal(value: Any) -> Optional[Decimal]:
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return amount if amount > 0 else None


def _parse_product_key(product_key: str) -> Dict[str, Optional[str]]:
    parts = _clean(product_key).split("::", 3)
    if len(parts) != 4 or parts[0] != "prod":
        return {"merchant_id": None, "platform": None, "source_product_id": None}
    return {
        "merchant_id": parts[1] or None,
        "platform": parts[2] or None,
        "source_product_id": parts[3] or None,
    }


def _iter_variant_payloads(*products: Mapping[str, Any]) -> List[Dict[str, Any]]:
    variants: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for product in products:
        for variant in _json_list(product.get("variants")):
            if not isinstance(variant, dict):
                continue
            key = "|".join(
                _clean(variant.get(field))
                for field in ("variant_id", "id", "sku")
            )
            if key in seen:
                continue
            seen.add(key)
            variants.append(variant)
    return variants


def _variant_id(value: Mapping[str, Any]) -> Optional[str]:
    return _first_non_empty(
        value.get("source_variant_id"),
        value.get("variant_id"),
        value.get("id"),
    )


def _variant_matches(variant: Mapping[str, Any], source_variant_id: str) -> bool:
    return _clean(_variant_id(variant)) == _clean(source_variant_id)


def _resolve_variant_identity(
    *,
    offer_payload: Mapping[str, Any],
    product_payload: Mapping[str, Any],
    cache_product_data: Mapping[str, Any],
    source_product_id: str,
) -> Tuple[Optional[str], Optional[Dict[str, Any]], Optional[str]]:
    variants = _iter_variant_payloads(cache_product_data, product_payload)
    offered_variant_id = _variant_id(offer_payload)
    if offered_variant_id:
        matches = [variant for variant in variants if _variant_matches(variant, offered_variant_id)]
        if len(matches) == 1:
            return offered_variant_id, matches[0], None
        if len(matches) > 1:
            return None, None, SOURCE_VARIANT_ID_AMBIGUOUS
        if variants:
            return None, None, SOURCE_VARIANT_ID_CONFLICT
        return offered_variant_id, None, None

    offered_sku = _first_non_empty(offer_payload.get("sku"))
    if offered_sku and variants:
        matches = [
            variant
            for variant in variants
            if _clean(variant.get("sku")) == offered_sku
        ]
        if len(matches) == 1:
            return _variant_id(matches[0]), matches[0], None
        if len(matches) > 1:
            return None, None, SOURCE_VARIANT_ID_AMBIGUOUS

    if len(variants) == 1:
        only_variant_id = _variant_id(variants[0])
        if only_variant_id:
            return only_variant_id, variants[0], None

    if not variants and source_product_id:
        return source_product_id, None, None

    return None, None, SOURCE_VARIANT_ID_MISSING


def _build_repair_plan(row: Mapping[str, Any]) -> RepairPlan:
    offer_payload = _json_dict(row.get("offer_payload"))
    product_payload = _json_dict(row.get("product_payload"))
    cache_product_data = _json_dict(row.get("cache_product_data"))
    product_key = _clean(row.get("product_key"))
    parsed_product_key = _parse_product_key(product_key)
    offer_id = _clean(row.get("offer_id"))
    sku_key = _clean(row.get("sku_key"))
    merchant_id = _first_non_empty(row.get("merchant_id"), parsed_product_key.get("merchant_id"))
    platform = _first_non_empty(
        row.get("product_platform"),
        product_payload.get("platform"),
        cache_product_data.get("platform"),
        parsed_product_key.get("platform"),
    )
    source_product_id = _first_non_empty(
        row.get("catalog_source_product_id"),
        offer_payload.get("product_id"),
        offer_payload.get("source_product_id"),
        product_payload.get("product_id"),
        product_payload.get("id"),
        cache_product_data.get("product_id"),
        cache_product_data.get("id"),
        parsed_product_key.get("source_product_id"),
    )

    sample = {
        "offer_id": offer_id,
        "sku_key": sku_key,
        "product_key": product_key,
        "merchant_id": merchant_id,
        "platform": platform,
        "source_product_id": source_product_id,
    }

    def suppress(reason: str) -> RepairPlan:
        return RepairPlan(
            action="suppress",
            reason=reason,
            offer_id=offer_id,
            sku_key=sku_key,
            source_identity=None,
            sku_values=None,
            sample={**sample, "reason": reason},
        )

    if not product_key or not row.get("product_title"):
        return suppress(CATALOG_PRODUCT_MISSING)
    if not merchant_id or not platform or not source_product_id:
        return suppress(PRODUCT_IDENTITY_MISSING)
    if _positive_decimal(row.get("list_price")) is None:
        return suppress(POSITIVE_LIST_PRICE_MISSING)

    source_variant_id, variant_payload, variant_reason = _resolve_variant_identity(
        offer_payload=offer_payload,
        product_payload=product_payload,
        cache_product_data=cache_product_data,
        source_product_id=source_product_id,
    )
    if variant_reason:
        return suppress(variant_reason)
    if not source_variant_id:
        return suppress(SOURCE_VARIANT_ID_MISSING)

    title = _first_non_empty(
        (variant_payload or {}).get("title") if variant_payload else None,
        row.get("product_title"),
        product_payload.get("title"),
        cache_product_data.get("title"),
    )
    if not title:
        return suppress(SKU_TITLE_MISSING)

    sku_values = {
        "sku_key": sku_key,
        "product_key": product_key,
        "merchant_id": merchant_id,
        "platform": platform,
        "source_product_id": source_product_id,
        "source_variant_id": source_variant_id,
        "sku": _first_non_empty(
            (variant_payload or {}).get("sku") if variant_payload else None,
            offer_payload.get("sku"),
            product_payload.get("sku"),
            cache_product_data.get("sku"),
        ),
        "barcode": _first_non_empty(
            (variant_payload or {}).get("barcode") if variant_payload else None,
            product_payload.get("barcode"),
            cache_product_data.get("barcode"),
        ),
        "title": title,
        "currency": _first_non_empty(
            row.get("offer_currency"),
            (variant_payload or {}).get("currency") if variant_payload else None,
            product_payload.get("currency"),
            cache_product_data.get("currency"),
        ),
        "image_url": _first_non_empty(
            (variant_payload or {}).get("image_url") if variant_payload else None,
            row.get("product_image_url"),
            product_payload.get("image_url"),
            cache_product_data.get("image_url"),
        ),
        "visible_attributes": json.dumps(
            product_payload.get("visible_attributes")
            or cache_product_data.get("visible_attributes")
            or {},
            default=_json_default,
        ),
        "visible_option_labels": json.dumps(
            _json_list((variant_payload or {}).get("visible_option_labels")) if variant_payload else [],
            default=_json_default,
        ),
        "ingredient_ids": json.dumps(
            _json_list(product_payload.get("ingredient_ids") or cache_product_data.get("ingredient_ids")),
            default=_json_default,
        ),
        "sku_payload": json.dumps(
            {
                "source": "orphan_shopify_offer_repair_v1",
                "repaired_from_offer_id": offer_id,
                "offer_payload": offer_payload,
                "variant_payload": variant_payload or {},
            },
            default=_json_default,
        ),
        "readiness_tier": _first_non_empty(row.get("product_readiness_tier"), row.get("offer_readiness_tier")) or "commerce_ready",
    }
    identity = (merchant_id, platform, product_key, source_variant_id)
    return RepairPlan(
        action="repair",
        reason=None,
        offer_id=offer_id,
        sku_key=sku_key,
        source_identity=identity,
        sku_values=sku_values,
        sample={
            **sample,
            "source_variant_id": source_variant_id,
            "title": title,
            "list_price": str(row.get("list_price")),
        },
    )


async def _fetch_orphan_offers(limit: int, *, db: Any = database) -> List[Dict[str, Any]]:
    limit_clause = "LIMIT :limit" if limit > 0 else ""
    params: Dict[str, Any] = {}
    if limit > 0:
        params["limit"] = int(limit)
    rows = await db.fetch_all(
        ORPHAN_SHOPIFY_OFFERS_QUERY.format(limit_clause=limit_clause),
        params,
    )
    return [dict(row) for row in rows or []]


async def _has_source_identity_conflict(plan: RepairPlan, *, db: Any = database) -> Optional[str]:
    if not plan.sku_values:
        return None
    row = await db.fetch_one(
        EXISTING_SOURCE_IDENTITY_SKU_QUERY,
        {
            "merchant_id": plan.sku_values["merchant_id"],
            "platform": plan.sku_values["platform"],
            "product_key": plan.sku_values["product_key"],
            "source_variant_id": plan.sku_values["source_variant_id"],
        },
    )
    if not row:
        return None
    existing_sku_key = _clean(dict(row).get("sku_key"))
    if existing_sku_key and existing_sku_key != plan.sku_key:
        return existing_sku_key
    return None


def _with_suppression(plan: RepairPlan, reason: str) -> RepairPlan:
    return RepairPlan(
        action="suppress",
        reason=reason,
        offer_id=plan.offer_id,
        sku_key=plan.sku_key,
        source_identity=plan.source_identity,
        sku_values=None,
        sample={**plan.sample, "reason": reason},
    )


async def _build_plans(rows: Iterable[Mapping[str, Any]], *, db: Any = database) -> List[RepairPlan]:
    plans: List[RepairPlan] = []
    seen_identities: Dict[Tuple[str, str, str, str], str] = {}
    for row in rows:
        plan = _build_repair_plan(row)
        if plan.action == "repair":
            conflict_sku_key = await _has_source_identity_conflict(plan, db=db)
            if conflict_sku_key:
                plan = _with_suppression(
                    plan,
                    SKU_IDENTITY_CONFLICT,
                )
                plan.sample["existing_sku_key"] = conflict_sku_key
            elif plan.source_identity in seen_identities and seen_identities[plan.source_identity] != plan.sku_key:
                plan = _with_suppression(plan, DUPLICATE_SOURCE_IDENTITY_IN_BATCH)
                plan.sample["first_sku_key"] = seen_identities.get(plan.source_identity)
            elif plan.source_identity is not None:
                seen_identities[plan.source_identity] = plan.sku_key
        plans.append(plan)
    return plans


@contextlib.asynccontextmanager
async def _maybe_transaction(db: Any) -> AsyncIterator[None]:
    transaction = getattr(db, "transaction", None)
    if not callable(transaction):
        yield
        return
    async with transaction():
        yield


async def _fetch_postcheck(*, db: Any = database) -> Dict[str, Any]:
    row = await db.fetch_one(POSTCHECK_SQL, {})
    return dict(row or {})


def _report_hash(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, default=_json_default).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


async def _drive(args: argparse.Namespace, *, db: Any = database) -> Dict[str, Any]:
    if args.apply and args.confirm != CONFIRM_TOKEN:
        raise SystemExit(f"--apply requires --confirm {CONFIRM_TOKEN}")

    if not getattr(db, "is_connected", False):
        connect = getattr(db, "connect", None)
        if callable(connect):
            await connect()

    rows = await _fetch_orphan_offers(args.limit, db=db)
    plans = await _build_plans(rows, db=db)
    action_counts = Counter(plan.action for plan in plans)
    reason_counts = Counter(plan.reason for plan in plans if plan.reason)

    repair_samples = [plan.sample for plan in plans if plan.action == "repair"][: args.sample_limit]
    suppression_samples = [plan.sample for plan in plans if plan.action == "suppress"][: args.sample_limit]
    planned_report = {
        "apply": bool(args.apply),
        "limit": args.limit,
        "targets_scanned": len(rows),
        "planned_actions": dict(action_counts),
        "planned_suppression_reasons": dict(reason_counts),
        "repair_samples": repair_samples,
        "suppression_samples": suppression_samples,
    }
    report_hash = _report_hash(planned_report)

    applied = {
        "sku_inserts_attempted": 0,
        "offers_suppressed": 0,
        "writer_audit_logged": False,
    }
    if args.apply and plans:
        audit = WriterAuditAccumulator(
            writer_name=REPAIR_WRITER_NAME,
            batch_id=args.batch_id or f"{REPAIR_WRITER_NAME}:{report_hash[:12]}",
            dry_run_report_hash=report_hash,
        )
        async with _maybe_transaction(db):
            inserted_sku_keys: set[str] = set()
            for plan in plans:
                if plan.action != "repair" or not plan.sku_values:
                    continue
                if plan.sku_key in inserted_sku_keys:
                    continue
                await db.execute(INSERT_REPAIR_SKU_SQL, plan.sku_values)
                inserted_sku_keys.add(plan.sku_key)
                applied["sku_inserts_attempted"] += 1

            for plan in plans:
                if plan.action != "suppress" or not plan.reason:
                    continue
                await db.execute(
                    SUPPRESS_ORPHAN_OFFER_SQL,
                    {
                        "offer_id": plan.offer_id,
                        "suppression_reason": plan.reason,
                    },
                )
                applied["offers_suppressed"] += 1

            audit.record_applied(action_counts.get("repair", 0))
            audit.record_skips(dict(reason_counts))
            await write_writer_audit_log(audit, actor=REPAIR_WRITER_NAME, db=db)
            applied["writer_audit_logged"] = True

    postcheck = await _fetch_postcheck(db=db) if args.postcheck else {}
    return {
        **planned_report,
        "dry_run_report_hash": report_hash,
        "applied": applied,
        "postcheck": postcheck,
        "safety": {
            "deletes": 0,
            "price_or_availability_fallbacks": 0,
            "serving_eligibility_updates": 0,
        },
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write deterministic SKU repairs and suppress unresolved offers. Default: dry-run.",
    )
    parser.add_argument(
        "--confirm",
        default="",
        help=f"Required with --apply. Must equal {CONFIRM_TOKEN}.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=500,
        help="Max orphan Shopify offers to classify (0 = all). Default 500.",
    )
    parser.add_argument(
        "--sample-limit",
        type=int,
        default=10,
        help="Max repair/suppression samples to include in the report. Default 10.",
    )
    parser.add_argument(
        "--batch-id",
        default="",
        help="Optional writer_audit_log batch_id. Defaults to report hash.",
    )
    parser.add_argument(
        "--no-postcheck",
        dest="postcheck",
        action="store_false",
        help="Skip the read-only postcheck counts.",
    )
    parser.set_defaults(postcheck=True)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    report = asyncio.run(_drive(args))
    print(json.dumps(report, indent=2, ensure_ascii=False, default=_json_default))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
