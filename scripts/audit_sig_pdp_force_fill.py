#!/usr/bin/env python3
"""Audit public sig_* PDP rows for the force-filled PDP contract.

Read-only. Joins catalog_products -> external_product_seeds ->
catalog_skus/catalog_offers and classifies each public signature into a
repair bucket. This script intentionally does not treat fallback,
synthetic, or underfilled content as success.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db.database import database


CORE_FIELDS = (
    "gallery",
    "description",
    "offers",
    "variant_clarity",
    "details",
    "ingredients",
    "how_to",
    "pivota_insights",
    "reviews",
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=0, help="0 = all rows")
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--brand", default=None)
    parser.add_argument(
        "--external-product-ids",
        default=None,
        help="Comma/newline separated source_product_id/external_product_id filter.",
    )
    parser.add_argument(
        "--all-sources",
        action="store_true",
        help="Audit all sig rows. Default focuses external_seed mirrored rows.",
    )
    parser.add_argument("--sample-limit", type=int, default=30)
    parser.add_argument("--out-json", default=None)
    parser.add_argument("--out-md", default=None)
    return parser.parse_args()


def _is_obj(value: Any) -> bool:
    return isinstance(_decode_jsonish(value), dict)


def _decode_jsonish(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text or text[0] not in "{[":
        return value
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return value


def _as_obj(value: Any) -> Dict[str, Any]:
    decoded = _decode_jsonish(value)
    return decoded if isinstance(decoded, dict) else {}


def _as_list(value: Any) -> List[Any]:
    decoded = _decode_jsonish(value)
    if isinstance(decoded, list):
        return decoded
    if decoded:
        return [decoded]
    return []


def _text(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
        if value is not None and not isinstance(value, (dict, list)):
            text = str(value).strip()
            if text:
                return text
    return ""


def _list_len(*values: Any) -> int:
    for value in values:
        decoded = _decode_jsonish(value)
        if isinstance(decoded, list):
            return len([item for item in decoded if item])
        if isinstance(decoded, dict):
            return len(decoded)
        if isinstance(decoded, str) and decoded.strip():
            return 1
    return 0


def _extract_seed_data(row: Dict[str, Any]) -> Dict[str, Any]:
    seed_data = _as_obj(row.get("seed_data"))
    payload = _as_obj(row.get("product_payload"))
    if seed_data:
        return seed_data
    return _as_obj(payload.get("seed_data"))


def _has_review_summary(
    seed_data: Dict[str, Any],
    snapshot: Dict[str, Any],
    identity_review_summary: Dict[str, Any],
) -> bool:
    review_summary = _as_obj(
        seed_data.get("review_summary")
        or snapshot.get("review_summary")
        or identity_review_summary
    )
    if not review_summary:
        return False
    if _text(review_summary.get("status")).lower() in {"none", "no_reviews", "unavailable"}:
        return True
    if (
        review_summary.get("aggregate")
        or review_summary.get("distribution")
        or review_summary.get("star_distribution")
        or review_summary.get("rating_distribution")
    ):
        return True
    if review_summary.get("rating") is not None and (
        review_summary.get("review_count") is not None
        or review_summary.get("exact_item_review_count") is not None
        or review_summary.get("product_line_review_count") is not None
    ):
        return True
    previews = (
        review_summary.get("reviews")
        or review_summary.get("preview")
        or review_summary.get("preview_items")
        or review_summary.get("items")
    )
    return _list_len(previews) > 0


def _has_insights(
    seed_data: Dict[str, Any],
    snapshot: Dict[str, Any],
    product_intel_kb_analysis: Dict[str, Any],
) -> bool:
    kb_bundle = _as_obj(product_intel_kb_analysis.get("product_intel_v1") or product_intel_kb_analysis.get("product_intel"))
    candidates = [
        seed_data.get("product_intel"),
        seed_data.get("pivota_insights"),
        seed_data.get("intel_bundle"),
        snapshot.get("product_intel"),
        snapshot.get("pivota_insights"),
        snapshot.get("intel_bundle"),
        kb_bundle,
        product_intel_kb_analysis if _text(product_intel_kb_analysis.get("contract_version")) == "pivota.product_intel.v1" else None,
    ]
    for candidate in candidates:
        candidate = _decode_jsonish(candidate)
        if isinstance(candidate, dict) and candidate:
            status = _text(candidate.get("status"), candidate.get("review_state"), candidate.get("state")).lower()
            if not status or status in {"reviewed", "published", "ready", "approved"}:
                return True
        if isinstance(candidate, list) and candidate:
            return True
    return False


def _classify_row(row: Dict[str, Any]) -> Dict[str, Any]:
    seed_data = _extract_seed_data(row)
    snapshot = _as_obj(seed_data.get("snapshot"))
    quarantine = _as_obj(seed_data.get("snapshot_quarantine"))
    product_payload = _as_obj(row.get("product_payload"))
    product_intel_kb_analysis = _as_obj(row.get("product_intel_kb_analysis"))
    identity_review_summary = _as_obj(row.get("identity_review_summary"))

    gallery_count = _list_len(
        snapshot.get("image_urls"),
        snapshot.get("images"),
        seed_data.get("image_urls"),
        seed_data.get("images"),
        row.get("catalog_image_url"),
        row.get("seed_image_url"),
    )
    variant_count = _list_len(
        snapshot.get("variants"),
        seed_data.get("variants"),
        product_payload.get("variants"),
    )
    offer_count = int(row.get("offer_count") or 0)
    sku_count = int(row.get("sku_count") or 0)
    has_seed_offer = bool(row.get("seed_price_amount") is not None and _text(row.get("seed_price_currency")))
    details_count = _list_len(
        seed_data.get("pdp_details_sections"),
        snapshot.get("pdp_details_sections"),
        snapshot.get("details_sections"),
        snapshot.get("details"),
    )
    ingredient_count = _list_len(
        seed_data.get("raw_ingredient_text_clean"),
        seed_data.get("pdp_ingredients_raw"),
        seed_data.get("inci_list"),
        _as_obj(seed_data.get("ingredient_intel")).get("inci_list"),
        snapshot.get("raw_ingredient_text_clean"),
        snapshot.get("pdp_ingredients_raw"),
        snapshot.get("inci_list"),
    )
    how_to_count = _list_len(
        seed_data.get("pdp_how_to_use_raw"),
        seed_data.get("how_to"),
        seed_data.get("how_to_use"),
        snapshot.get("pdp_how_to_use_raw"),
        snapshot.get("how_to"),
        snapshot.get("how_to_use"),
    )
    description = _text(
        row.get("catalog_description"),
        row.get("seed_description"),
        seed_data.get("description"),
        snapshot.get("description"),
        seed_data.get("pdp_description_raw"),
        snapshot.get("pdp_description_raw"),
    )

    module_status = {
        "gallery": gallery_count > 0,
        "description": len(description) >= 80,
        "offers": offer_count > 0 or has_seed_offer,
        "variant_clarity": variant_count > 0 or sku_count > 0,
        "details": details_count > 0,
        "ingredients": ingredient_count > 0,
        "how_to": how_to_count > 0,
        "pivota_insights": _has_insights(seed_data, snapshot, product_intel_kb_analysis),
        "reviews": _has_review_summary(seed_data, snapshot, identity_review_summary),
    }
    missing = [field for field in CORE_FIELDS if not module_status.get(field)]

    has_approved_snapshot = bool(
        seed_data.get("external_seed_snapshot_contract")
        or snapshot.get("external_seed_snapshot_contract")
        or seed_data.get("pdp_field_quality_summary")
        or snapshot.get("pdp_field_quality_summary")
    )
    has_seed = bool(row.get("seed_id"))
    is_external_seed = (
        _text(row.get("merchant_id")) == "external_seed"
        and _text(row.get("platform")) == "external_seed"
    )

    if not missing:
        bucket = "filled"
    elif is_external_seed and has_seed and has_approved_snapshot and any(
        module_status[field] for field in ("gallery", "description", "variant_clarity", "details", "ingredients", "how_to")
    ):
        bucket = "projectable_from_seed"
    elif quarantine or _text(row.get("brand")).lower() == "fenty beauty":
        bucket = "restore_from_history"
    elif is_external_seed and has_seed and _text(row.get("seed_canonical_url"), row.get("destination_url")):
        bucket = "needs_source_backfill"
    elif has_seed:
        bucket = "needs_manual_review"
    else:
        bucket = "source_unrecoverable_blocker"

    return {
        "sig_id": row.get("pivota_signature_id"),
        "canonical_url": row.get("pivota_canonical_url"),
        "product_key": row.get("product_key"),
        "merchant_id": row.get("merchant_id"),
        "platform": row.get("platform"),
        "source_product_id": row.get("source_product_id"),
        "seed_id": row.get("seed_id"),
        "brand": row.get("brand") or row.get("seed_brand"),
        "title": row.get("catalog_title") or row.get("seed_title"),
        "domain": row.get("seed_domain"),
        "module_status": module_status,
        "missing": missing,
        "bucket": bucket,
        "counts": {
            "gallery": gallery_count,
            "variants": variant_count,
            "skus": sku_count,
            "offers": offer_count,
            "details": details_count,
            "ingredients": ingredient_count,
            "how_to": how_to_count,
        },
        "has_approved_snapshot": has_approved_snapshot,
        "has_quarantine": bool(quarantine),
        "has_product_intel_kb": bool(product_intel_kb_analysis),
        "has_identity_review_summary": bool(identity_review_summary),
    }


def _build_where(args: argparse.Namespace) -> tuple[str, Dict[str, Any]]:
    conditions = ["cp.pivota_signature_id IS NOT NULL"]
    values: Dict[str, Any] = {"offset": max(0, args.offset)}
    if not args.all_sources:
        conditions.append("cp.merchant_id = 'external_seed'")
        conditions.append("cp.platform = 'external_seed'")
    if args.brand:
        conditions.append("lower(coalesce(cp.brand, eps.seed_data->>'brand', eps.seed_data->'snapshot'->>'brand', '')) = lower(:brand)")
        values["brand"] = args.brand.strip()
    external_ids = [
        item.strip()
        for item in (args.external_product_ids or "").replace("\n", ",").split(",")
        if item.strip()
    ]
    if external_ids:
        conditions.append("cp.source_product_id = ANY(:external_product_ids)")
        values["external_product_ids"] = external_ids
    return " AND ".join(conditions), values


async def _fetch_rows(args: argparse.Namespace) -> List[Dict[str, Any]]:
    where_sql, values = _build_where(args)
    limit_sql = ""
    if args.limit and args.limit > 0:
        limit_sql = "LIMIT :limit"
        values["limit"] = args.limit
    rows = await database.fetch_all(
        f"""
        SELECT
          cp.product_key,
          cp.merchant_id,
          cp.platform,
          cp.source_product_id,
          cp.source_system,
          cp.title AS catalog_title,
          cp.description AS catalog_description,
          cp.brand,
          cp.product_type,
          cp.category,
          cp.category_path,
          cp.image_url AS catalog_image_url,
          cp.product_payload,
          cp.pivota_signature_id,
          cp.pivota_canonical_url,
          cp.readiness_tier,
          cp.pdp_lifecycle_stage,
          eps.id AS seed_id,
          eps.external_product_id,
          eps.status AS seed_status,
          eps.market AS seed_market,
          eps.tool AS seed_tool,
          eps.domain AS seed_domain,
          eps.destination_url,
          eps.canonical_url AS seed_canonical_url,
          eps.title AS seed_title,
          eps.image_url AS seed_image_url,
          eps.price_amount AS seed_price_amount,
          eps.price_currency AS seed_price_currency,
          eps.availability AS seed_availability,
          eps.seed_data,
          eps.seed_data->>'brand' AS seed_brand,
          COALESCE(sku_counts.sku_count, 0) AS sku_count,
          COALESCE(offer_counts.offer_count, 0) AS offer_count,
          intel.analysis AS product_intel_kb_analysis,
          identity_reviews.review_summary AS identity_review_summary
        FROM catalog_products cp
        LEFT JOIN external_product_seeds eps
          ON cp.merchant_id = 'external_seed'
         AND cp.platform = 'external_seed'
         AND eps.external_product_id = cp.source_product_id
        LEFT JOIN LATERAL (
          SELECT count(DISTINCT cs.sku_key) AS sku_count
          FROM catalog_skus cs
          WHERE cs.product_key = cp.product_key
        ) sku_counts ON true
        LEFT JOIN LATERAL (
          SELECT count(DISTINCT co.offer_id) AS offer_count
          FROM catalog_offers co
          WHERE co.product_key = cp.product_key
        ) offer_counts ON true
        LEFT JOIN LATERAL (
          SELECT kb.analysis
          FROM aurora_product_intel_kb kb
          WHERE kb.kb_key IN (
            'product:' || cp.source_product_id,
            'product:' || cp.pivota_signature_id
          )
          ORDER BY
            CASE WHEN kb.kb_key = 'product:' || cp.source_product_id THEN 0 ELSE 1 END,
            kb.last_success_at DESC NULLS LAST,
            kb.updated_at DESC NULLS LAST
          LIMIT 1
        ) intel ON true
        LEFT JOIN LATERAL (
          SELECT pil.review_summary
          FROM pdp_identity_listing pil
          WHERE pil.merchant_id = cp.merchant_id
            AND pil.product_id = cp.source_product_id
            AND pil.review_summary IS NOT NULL
            AND pil.review_summary <> '{{}}'::jsonb
          ORDER BY pil.updated_at DESC NULLS LAST
          LIMIT 1
        ) identity_reviews ON true
        WHERE {where_sql}
        ORDER BY cp.updated_at DESC NULLS LAST, cp.product_key ASC
        {limit_sql}
        OFFSET :offset
        """,
        values,
    )
    return [dict(row) for row in rows]


def _summarize(items: Iterable[Dict[str, Any]], sample_limit: int) -> Dict[str, Any]:
    item_list = list(items)
    bucket_counts = Counter(item["bucket"] for item in item_list)
    missing_counts = Counter(field for item in item_list for field in item["missing"])
    brand_counts: Dict[str, Counter[str]] = defaultdict(Counter)
    for item in item_list:
        brand = _text(item.get("brand"), "unknown")
        brand_counts[brand][item["bucket"]] += 1
    top_brand_blockers = sorted(
        (
            {
                "brand": brand,
                "not_filled": sum(count for bucket, count in counts.items() if bucket != "filled"),
                "buckets": dict(counts),
            }
            for brand, counts in brand_counts.items()
        ),
        key=lambda row: (-row["not_filled"], row["brand"]),
    )[:20]
    blockers = [item for item in item_list if item["bucket"] != "filled"]
    return {
        "total": len(item_list),
        "filled": bucket_counts.get("filled", 0),
        "not_filled": len(blockers),
        "bucket_counts": dict(bucket_counts),
        "missing_counts": dict(missing_counts),
        "top_brand_blockers": top_brand_blockers,
        "samples": blockers[:sample_limit],
    }


def _render_md(report: Dict[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        "# SIG PDP Force-Fill Audit",
        "",
        f"- total: `{summary['total']}`",
        f"- filled: `{summary['filled']}`",
        f"- not_filled: `{summary['not_filled']}`",
        "",
        "## Buckets",
        "",
        "| Bucket | Count |",
        "| --- | ---: |",
    ]
    for bucket, count in sorted(summary["bucket_counts"].items(), key=lambda item: (-item[1], item[0])):
        lines.append(f"| `{bucket}` | {count} |")
    lines.extend(["", "## Missing Fields", "", "| Field | Count |", "| --- | ---: |"])
    for field, count in sorted(summary["missing_counts"].items(), key=lambda item: (-item[1], item[0])):
        lines.append(f"| `{field}` | {count} |")
    lines.extend(["", "## Top Brand Blockers", "", "| Brand | Not Filled | Buckets |", "| --- | ---: | --- |"])
    for row in summary["top_brand_blockers"]:
        lines.append(f"| {row['brand']} | {row['not_filled']} | `{json.dumps(row['buckets'], sort_keys=True)}` |")
    lines.extend(["", "## Sample Blockers", "", "| Sig | Brand | Missing | Bucket |", "| --- | --- | --- | --- |"])
    for item in summary["samples"]:
        lines.append(
            f"| `{item.get('sig_id')}` | {item.get('brand') or ''} | `{', '.join(item.get('missing') or [])}` | `{item.get('bucket')}` |"
        )
    lines.append("")
    return "\n".join(lines)


def _write(path: Optional[str], content: str) -> None:
    if not path:
        return
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(content, encoding="utf-8")


async def _run(args: argparse.Namespace) -> int:
    await database.connect()
    try:
        rows = await _fetch_rows(args)
    finally:
        await database.disconnect()
    items = [_classify_row(row) for row in rows]
    report = {
        "args": vars(args),
        "summary": _summarize(items, args.sample_limit),
        "items": items,
    }
    json_text = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    md_text = _render_md(report)
    _write(args.out_json, json_text)
    _write(args.out_md, md_text)
    print(md_text)
    return 0


def main() -> int:
    return asyncio.run(_run(_parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
