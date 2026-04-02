#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from db.database import database
from models.standard_product import StandardProduct
from services.catalog_sync_service import make_catalog_product_key, sync_products_cache_to_catalog


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Canonical catalog backfill and verify tool for products_cache -> catalog_*."
    )
    parser.add_argument("--merchant-id", required=True)
    parser.add_argument("--platform", default=None)
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument(
        "--mode",
        choices=("dry-run", "apply", "verify"),
        required=True,
    )
    parser.add_argument("--include-expired", action="store_true")
    parser.add_argument("--source-system", default="products_cache_backfill")
    parser.add_argument("--source-ref", default=None)
    parser.add_argument("--sample-limit", type=int, default=20)
    parser.add_argument("--output-json", default=None)
    parser.add_argument("--output-md", default=None)
    return parser.parse_args()


def _dry_run_summary(payloads: List[Dict[str, Any]], merchant_id: str, default_platform: Optional[str]) -> Dict[str, Any]:
    product_keys: List[str] = []
    variants_total = 0
    parse_failures = 0
    readiness_counter: Counter[str] = Counter()
    beauty_candidates = 0

    for payload in payloads:
        try:
            product = StandardProduct(**payload)
        except Exception:
            parse_failures += 1
            continue
        platform = str(payload.get("platform") or default_platform or "shopify").strip() or "shopify"
        product_id = str(product.product_id or product.id or "").strip()
        if not product_id:
            parse_failures += 1
            continue
        product_keys.append(make_catalog_product_key(merchant_id, platform, product_id))
        variants = list(product.variants or [])
        variants_total += max(1, len(variants))
        has_ingredients = bool(product.ingredient_ids)
        metadata = _json_dict(product.platform_metadata)
        has_how_to_use = bool(
            metadata.get("how_to_use")
            or metadata.get("howToUse")
            or metadata.get("usage")
            or metadata.get("usage_text")
            or metadata.get("directions")
            or metadata.get("directions_text")
        )
        if has_ingredients or product.visible_attributes:
            beauty_candidates += 1
        if has_ingredients and has_how_to_use:
            readiness_counter["knowledge_ready"] += 1
        elif has_ingredients or has_how_to_use:
            readiness_counter["vertical_ready"] += 1
        else:
            readiness_counter["commerce_ready"] += 1

    return {
        "products_cache_unique_products": len(product_keys),
        "products_cache_unique_product_keys": product_keys,
        "products_cache_variants_estimated": variants_total,
        "products_cache_parse_failures": parse_failures,
        "beauty_candidates_estimated": beauty_candidates,
        "readiness_estimate": dict(readiness_counter),
    }


async def _fetch_products_cache_payloads(
    *,
    merchant_id: str,
    platform: Optional[str],
    limit: int,
    include_expired: bool,
) -> List[Dict[str, Any]]:
    params: Dict[str, Any] = {"merchant_id": merchant_id}
    platform_clause = ""
    if platform:
        platform_clause = "AND platform = :platform"
        params["platform"] = platform
    expiry_clause = ""
    if not include_expired:
        expiry_clause = "AND expires_at > CURRENT_TIMESTAMP"
    limit_clause = ""
    if limit > 0:
        limit_clause = "LIMIT :limit"
        params["limit"] = limit

    rows = await database.fetch_all(
        f"""
        SELECT product_data
        FROM products_cache
        WHERE merchant_id = :merchant_id
        {platform_clause}
        {expiry_clause}
        ORDER BY cached_at DESC
        {limit_clause}
        """,
        params,
    )
    payloads: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        payload = _json_dict(dict(row).get("product_data"))
        product_id = str(payload.get("product_id") or payload.get("id") or "").strip()
        payload_platform = str(payload.get("platform") or platform or "shopify").strip() or "shopify"
        dedupe_key = f"{payload_platform}:{product_id}"
        if not product_id or dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        payloads.append(payload)
    return payloads


async def _verify_summary(
    *,
    merchant_id: str,
    platform: Optional[str],
    payloads: List[Dict[str, Any]],
    sample_limit: int,
) -> Dict[str, Any]:
    base_params: Dict[str, Any] = {"merchant_id": merchant_id}
    platform_params: Dict[str, Any] = {"merchant_id": merchant_id}
    platform_clause = ""
    if platform:
        platform_clause = "AND platform = :platform"
        platform_params["platform"] = platform

    async def _count(sql: str, sql_params: Dict[str, Any]) -> int:
        row = await database.fetch_one(sql, sql_params)
        data = dict(row) if row else {}
        return int(data.get("count") or 0)

    catalog_product_count = await _count(
        f"SELECT COUNT(*) AS count FROM catalog_products WHERE merchant_id = :merchant_id {platform_clause}",
        platform_params,
    )
    catalog_sku_count = await _count(
        f"SELECT COUNT(*) AS count FROM catalog_skus WHERE merchant_id = :merchant_id {platform_clause}",
        platform_params,
    )
    catalog_offer_count = await _count(
        "SELECT COUNT(*) AS count FROM catalog_offers WHERE merchant_id = :merchant_id",
        base_params,
    )
    beauty_profile_count = await _count(
        "SELECT COUNT(*) AS count FROM beauty_product_profiles WHERE merchant_id = :merchant_id",
        base_params,
    )
    quote_snapshot_count = await _count(
        "SELECT COUNT(*) AS count FROM catalog_quote_snapshots WHERE merchant_id = :merchant_id",
        base_params,
    )
    sync_job_count = await _count(
        "SELECT COUNT(*) AS count FROM catalog_sync_jobs WHERE merchant_id = :merchant_id",
        base_params,
    )

    expected_product_keys: List[str] = []
    for payload in payloads:
        payload_platform = str(payload.get("platform") or platform or "shopify").strip() or "shopify"
        product_id = str(payload.get("product_id") or payload.get("id") or "").strip()
        if not product_id:
            continue
        expected_product_keys.append(make_catalog_product_key(merchant_id, payload_platform, product_id))

    expected_product_keys = list(dict.fromkeys(expected_product_keys))
    existing_rows = await database.fetch_all(
        f"""
        SELECT product_key
        FROM catalog_products
        WHERE merchant_id = :merchant_id
        {platform_clause}
        """,
        platform_params,
    )
    existing_keys = {str(dict(row).get("product_key") or "").strip() for row in existing_rows}
    missing_keys = [key for key in expected_product_keys if key not in existing_keys]

    return {
        "catalog_products": catalog_product_count,
        "catalog_skus": catalog_sku_count,
        "catalog_offers": catalog_offer_count,
        "beauty_product_profiles": beauty_profile_count,
        "catalog_quote_snapshots": quote_snapshot_count,
        "catalog_sync_jobs": sync_job_count,
        "expected_product_keys": len(expected_product_keys),
        "missing_product_keys_count": len(missing_keys),
        "missing_product_keys_sample": missing_keys[: max(0, sample_limit)],
    }


def _markdown_report(result: Dict[str, Any]) -> str:
    lines = [
        "# Catalog Backfill Verify Report",
        "",
        f"- generated_at: `{result['generated_at']}`",
        f"- mode: `{result['mode']}`",
        f"- merchant_id: `{result['merchant_id']}`",
        f"- platform: `{result.get('platform') or 'all'}`",
        "",
        "## Summary",
        "",
    ]
    for key, value in (result.get("summary") or {}).items():
        lines.append(f"- {key}: `{json.dumps(value, ensure_ascii=True, default=str) if isinstance(value, (dict, list)) else value}`")
    return "\n".join(lines) + "\n"


async def _run(args: argparse.Namespace) -> Dict[str, Any]:
    await database.connect()
    try:
        payloads = await _fetch_products_cache_payloads(
            merchant_id=args.merchant_id,
            platform=args.platform,
            limit=args.limit,
            include_expired=bool(args.include_expired),
        )
        dry_run = _dry_run_summary(payloads, args.merchant_id, args.platform)

        apply_stats: Optional[Dict[str, Any]] = None
        if args.mode == "apply":
            apply_stats = await sync_products_cache_to_catalog(
                merchant_id=args.merchant_id,
                platform=args.platform,
                limit=args.limit,
                include_expired=bool(args.include_expired),
                source_system=args.source_system,
                source_ref=args.source_ref or f"{args.source_system}:{_utcnow_iso()}",
            )

        verify = None
        if args.mode in {"apply", "verify"}:
            verify = await _verify_summary(
                merchant_id=args.merchant_id,
                platform=args.platform,
                payloads=payloads,
                sample_limit=args.sample_limit,
            )

        summary: Dict[str, Any] = dict(dry_run)
        if apply_stats is not None:
            summary["apply_stats"] = apply_stats
        if verify is not None:
            summary["verify"] = verify

        return {
            "generated_at": _utcnow_iso(),
            "mode": args.mode,
            "merchant_id": args.merchant_id,
            "platform": args.platform,
            "summary": summary,
        }
    finally:
        await database.disconnect()


def _write_if_requested(path_str: Optional[str], content: str) -> None:
    if not path_str:
        return
    path = Path(path_str)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def main() -> int:
    args = _parse_args()
    result = asyncio.run(_run(args))
    json_blob = json.dumps(result, indent=2, ensure_ascii=False, default=str)
    markdown = _markdown_report(result)
    _write_if_requested(args.output_json, json_blob + "\n")
    _write_if_requested(args.output_md, markdown)
    print(json_blob)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
