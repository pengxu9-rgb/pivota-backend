#!/usr/bin/env python3
"""
Mirror active standalone external_product_seeds into catalog_products.

This is intentionally a narrow bridge for the canonical PDP migration:
  - source table: external_product_seeds
  - destination: catalog_products
  - identity tuple: (merchant_id='external_seed',
                     platform='external_seed',
                     source_product_id=external_product_id)

It is idempotent. Dry-run is the default; pass --apply to insert missing
catalog_products rows. Existing catalog_products rows are not overwritten.
Run scripts/backfill_pivota_canonical_pdp.py --apply afterwards to mint sig_*.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db.database import database
from services.pdp_category_classifier import resolve_path_from_row
from services.pdp_taxonomy import derive_taxonomy_v1


MERCHANT_ID = "external_seed"
PLATFORM = "external_seed"
CATALOG_TRACK = "external_referral"
TRUTH_TIER = "observed"
READINESS_TIER = "referral_only"
SOURCE_SYSTEM = "external_product_seeds_mirror_v1"
CATEGORY_CONFIDENCE_REGEX_AT_MIRROR = 0.85
CATEGORY_LABEL_SOURCE_AT_MIRROR = "regex_backfill_at_mirror"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Insert missing catalog_products rows. Default is dry-run.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Limit rows inserted / previewed (0 = all missing rows).",
    )
    parser.add_argument(
        "--sample-limit",
        type=int,
        default=20,
        help="Number of sample rows to include in the report.",
    )
    parser.add_argument("--output-json", default=None)
    parser.add_argument("--output-md", default=None)
    return parser.parse_args()


def _write_if_requested(path_str: Optional[str], content: str) -> None:
    if not path_str:
        return
    path = Path(path_str)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def resolve_mirror_category_metadata(
    *,
    category: Optional[str],
    product_type: Optional[str],
    title: Optional[str],
) -> Dict[str, Any]:
    """Classify only newly inserted mirror rows.

    Existing catalog_products rows are intentionally not overwritten by the
    mirror; this helper is used only on the INSERT path so future mirrors do not
    recreate NULL category_path rows.
    """
    hit = resolve_path_from_row(category=category, product_type=product_type, title=title)
    if hit is None:
        return {
            "category_path": None,
            "category_confidence": None,
            "category_label_source": None,
            "category_label": None,
        }
    label, path = hit
    return {
        "category_path": path,
        "category_confidence": CATEGORY_CONFIDENCE_REGEX_AT_MIRROR,
        "category_label_source": CATEGORY_LABEL_SOURCE_AT_MIRROR,
        "category_label": label,
    }


# Phase O-1 followup — common JSONB paths that scrapers / agents put
# tag-like data in, ordered most-trusted → least-trusted. The mirror
# returns the FIRST non-empty list it finds. Keeps "scraper schema
# drift" contained: any new scraper that uses a known path is
# automatically supported; a new path requires one extension here.
_SEED_DATA_TAG_PATHS: Tuple[Tuple[str, ...], ...] = (
    ("derived", "recall", "tags"),  # Pivota-derived recall doc
    ("snapshot", "tags"),            # Shopify-style scraped snapshot
    ("product", "tags"),             # generic scraper "product" wrapper
    ("tags",),                        # top-level
)


def _extract_tags_from_seed_data(seed_data: Any) -> List[str]:
    """Walk seed_data JSONB for tag-like content. Returns [] if none found
    (the same semantics ingest_standard_products uses on Path A: empty list
    means "we looked and found nothing", not "field doesn't exist").
    Always returns deduped, stripped, non-empty strings."""
    if not isinstance(seed_data, dict):
        return []
    for path in _SEED_DATA_TAG_PATHS:
        node: Any = seed_data
        for key in path:
            if not isinstance(node, dict):
                node = None
                break
            node = node.get(key)
            if node is None:
                break
        if node is None:
            continue
        # Accept either a list or a comma-separated string (some Shopify
        # snapshots flatten tags to a single string).
        if isinstance(node, list):
            values = node
        elif isinstance(node, str):
            values = node.split(",")
        else:
            continue
        out: List[str] = []
        for item in values:
            if isinstance(item, dict):
                candidate = str(item.get("name") or item.get("label") or "").strip()
            else:
                candidate = str(item or "").strip()
            if candidate and candidate not in out:
                out.append(candidate)
        if out:
            return out
    return []


async def _table_exists(name: str) -> bool:
    row = await database.fetch_one(
        "SELECT to_regclass(:table_name) AS regclass",
        {"table_name": f"public.{name}"},
    )
    return bool(row and row["regclass"])


async def _required_schema() -> Dict[str, Any]:
    required_tables = ["external_product_seeds", "catalog_products"]
    table_status = {table: await _table_exists(table) for table in required_tables}

    index_rows = await database.fetch_all(
        """
        SELECT indexname, indexdef
        FROM pg_indexes
        WHERE schemaname = 'public'
          AND tablename = 'catalog_products'
          AND indexdef ILIKE '%merchant_id%'
          AND indexdef ILIKE '%platform%'
          AND indexdef ILIKE '%source_product_id%'
          AND indexdef ILIKE '%UNIQUE%'
        ORDER BY indexname
        """
    )
    identity_unique_indexes = [dict(row) for row in index_rows]
    return {
        "tables": table_status,
        "identity_unique_indexes": identity_unique_indexes,
        "ok": all(table_status.values()) and bool(identity_unique_indexes),
    }


COMMON_CTES = """
WITH active_standalone AS (
  SELECT *
  FROM external_product_seeds
  WHERE lower(coalesce(status, '')) = 'active'
    AND coalesce(attached_product_key, '') = ''
),
ranked AS (
  SELECT
    eps.*,
    row_number() OVER (
      PARTITION BY eps.external_product_id
      ORDER BY
        CASE WHEN eps.market = 'US' THEN 0 ELSE 1 END,
        eps.updated_at DESC NULLS LAST,
        eps.created_at DESC NULLS LAST,
        eps.id ASC
    ) AS rn,
    count(*) OVER (PARTITION BY eps.external_product_id) AS duplicate_count,
    nullif(
      btrim(
        coalesce(
          eps.seed_data#>>'{snapshot,description}',
          eps.seed_data#>>'{snapshot,description_text}',
          eps.seed_data#>>'{snapshot,pdp_description}',
          eps.seed_data#>>'{snapshot,pdp_description_raw}',
          eps.seed_data#>>'{snapshot,overview}',
          eps.seed_data#>>'{snapshot,summary}',
          eps.seed_data->>'description',
          eps.seed_data->>'description_text',
          eps.seed_data->>'pdp_description',
          eps.seed_data->>'pdp_description_raw',
          eps.seed_data->>'overview',
          eps.seed_data->>'summary',
          ''
        )
      ),
      ''
    ) AS mirrored_description,
    nullif(
      btrim(
        coalesce(
          eps.seed_data#>>'{snapshot,brand}',
          eps.seed_data->>'brand',
          eps.seed_data#>>'{snapshot,vendor}',
          eps.seed_data->>'vendor',
          ''
        )
      ),
      ''
    ) AS mirrored_brand,
    nullif(
      btrim(
        coalesce(
          eps.seed_data#>>'{snapshot,product_type}',
          eps.seed_data->>'product_type',
          eps.seed_data#>>'{snapshot,kind}',
          eps.seed_data->>'kind',
          ''
        )
      ),
      ''
    ) AS mirrored_product_type,
    nullif(
      btrim(
        coalesce(
          eps.seed_data#>>'{snapshot,category}',
          eps.seed_data->>'category',
          eps.seed_data#>>'{recall_doc,recall_category}',
          eps.seed_data#>>'{derived,recall_category}',
          ''
        )
      ),
      ''
    ) AS mirrored_category
  FROM active_standalone eps
  WHERE nullif(btrim(coalesce(eps.external_product_id, '')), '') IS NOT NULL
),
candidates AS (
  SELECT *
  FROM ranked
  WHERE rn = 1
    AND length(external_product_id) <= 128
    AND length('prod::external_seed::external_seed::' || external_product_id) <= 255
    AND nullif(btrim(coalesce(title, '')), '') IS NOT NULL
),
missing AS (
  SELECT c.*
  FROM candidates c
  LEFT JOIN catalog_products cp
    ON cp.merchant_id = 'external_seed'
   AND cp.platform = 'external_seed'
   AND cp.source_product_id = c.external_product_id
  WHERE cp.product_key IS NULL
)
"""


async def _fetch_scalar(sql: str, values: Optional[Dict[str, Any]] = None) -> int:
    value = await database.fetch_val(sql, values or {})
    return int(value or 0)


async def _build_report(*, sample_limit: int, limit: int, apply: bool) -> Dict[str, Any]:
    schema = await _required_schema()
    if not schema["ok"]:
        return {
            "ok": False,
            "apply": apply,
            "schema": schema,
            "error": "required tables or catalog_products identity unique index missing",
        }

    totals_sql = (
        COMMON_CTES
        + """
        SELECT
          (SELECT count(*) FROM external_product_seeds) AS external_total,
          (SELECT count(*) FROM external_product_seeds WHERE lower(coalesce(status, '')) = 'active') AS external_active,
          (SELECT count(*) FROM active_standalone) AS active_standalone,
          (SELECT count(*) FROM active_standalone WHERE nullif(btrim(coalesce(external_product_id, '')), '') IS NULL) AS active_standalone_missing_external_product_id,
          (SELECT count(*) FROM ranked WHERE duplicate_count > 1) AS duplicate_active_standalone_rows,
          (SELECT count(*) FROM (SELECT external_product_id FROM ranked GROUP BY external_product_id HAVING count(*) > 1) d) AS duplicate_active_standalone_groups,
          (SELECT count(*) FROM ranked WHERE rn = 1 AND length(external_product_id) > 128) AS skipped_source_product_id_too_long,
          (SELECT count(*) FROM ranked WHERE rn = 1 AND length('prod::external_seed::external_seed::' || external_product_id) > 255) AS skipped_product_key_too_long,
          (SELECT count(*) FROM ranked WHERE rn = 1 AND nullif(btrim(coalesce(title, '')), '') IS NULL) AS skipped_missing_title,
          (SELECT count(*) FROM candidates) AS deduped_valid_candidates,
          (SELECT count(*) FROM candidates WHERE nullif(btrim(coalesce(image_url, '')), '') IS NOT NULL) AS candidates_with_image,
          (SELECT count(*) FROM candidates WHERE length(coalesce(mirrored_description, '')) >= 50) AS candidates_with_description_50,
          (SELECT count(*) FROM candidates WHERE nullif(btrim(coalesce(image_url, '')), '') IS NOT NULL AND length(coalesce(mirrored_description, '')) >= 50) AS candidates_visible_quality_ready,
          (SELECT count(*) FROM missing) AS missing_catalog_products,
          (SELECT count(*) FROM catalog_products) AS catalog_products_total,
          (SELECT count(*) FROM catalog_products WHERE pivota_signature_id IS NOT NULL) AS catalog_products_with_sig,
          (SELECT count(*) FROM catalog_products WHERE merchant_id = 'external_seed' AND platform = 'external_seed') AS catalog_products_external_seed,
          (SELECT count(*) FROM catalog_products WHERE merchant_id = 'external_seed' AND platform = 'external_seed' AND pivota_signature_id IS NOT NULL) AS catalog_products_external_seed_with_sig,
          (SELECT count(*) FROM catalog_products WHERE merchant_id = 'external_seed' AND platform = 'external_seed' AND coalesce(source_system, '') <> 'external_product_seeds_mirror_v1') AS legacy_external_seed_catalog_rows,
          (SELECT count(*) FROM catalog_products WHERE pivota_signature_id IS NOT NULL AND image_url IS NOT NULL AND length(coalesce(image_url, '')) > 0 AND length(coalesce(description, '')) >= 50) AS catalog_products_visible_quality_with_sig
        """
    )
    totals_row = await database.fetch_one(totals_sql)
    totals = dict(totals_row or {})

    sample_values = {"sample_limit": max(0, sample_limit)}
    missing_sample_rows = await database.fetch_all(
        COMMON_CTES
        + """
        SELECT
          id,
          external_product_id,
          market,
          tool,
          domain,
          title,
          destination_url,
          image_url,
          length(coalesce(mirrored_description, '')) AS description_length,
          duplicate_count
        FROM missing
        ORDER BY updated_at DESC NULLS LAST, id ASC
        LIMIT :sample_limit
        """,
        sample_values,
    )

    duplicate_sample_rows = await database.fetch_all(
        COMMON_CTES
        + """
        SELECT
          external_product_id,
          count(*) AS rows,
          array_agg(id ORDER BY updated_at DESC NULLS LAST, id ASC) AS seed_ids
        FROM ranked
        GROUP BY external_product_id
        HAVING count(*) > 1
        ORDER BY rows DESC, external_product_id ASC
        LIMIT :sample_limit
        """,
        sample_values,
    )

    by_market_rows = await database.fetch_all(
        COMMON_CTES
        + """
        SELECT market, count(*) AS rows
        FROM candidates
        GROUP BY market
        ORDER BY rows DESC, market ASC
        LIMIT :sample_limit
        """,
        sample_values,
    )

    by_domain_rows = await database.fetch_all(
        COMMON_CTES
        + """
        SELECT coalesce(nullif(lower(domain), ''), 'unknown') AS domain, count(*) AS rows
        FROM candidates
        GROUP BY 1
        ORDER BY rows DESC, domain ASC
        LIMIT :sample_limit
        """,
        sample_values,
    )

    report: Dict[str, Any] = {
        "ok": True,
        "apply": apply,
        "limit": limit,
        "schema": schema,
        "totals": totals,
        "missing_sample": [dict(row) for row in missing_sample_rows],
        "duplicate_sample": [dict(row) for row in duplicate_sample_rows],
        "candidate_by_market": [dict(row) for row in by_market_rows],
        "candidate_by_domain": [dict(row) for row in by_domain_rows],
    }
    return report


async def _apply(limit: int) -> int:
    limit_clause = ""
    values: Dict[str, Any] = {
        "merchant_id": MERCHANT_ID,
        "platform": PLATFORM,
        "catalog_track": CATALOG_TRACK,
        "truth_tier": TRUTH_TIER,
        "readiness_tier": READINESS_TIER,
        "source_system": SOURCE_SYSTEM,
    }
    if limit > 0:
        limit_clause = "LIMIT :limit"
        values["limit"] = limit

    rows = await database.fetch_all(
        COMMON_CTES
        + f"""
        SELECT
          'prod::external_seed::external_seed::' || external_product_id AS product_key,
          id,
          external_product_id,
          market,
          tool,
          domain,
          title,
          destination_url,
          canonical_url,
          price_amount,
          price_currency,
          availability,
          image_url,
          seed_data,
          updated_at,
          duplicate_count,
          rn,
          mirrored_description,
          mirrored_brand,
          mirrored_product_type,
          mirrored_category
        FROM missing
        ORDER BY updated_at DESC NULLS LAST, id ASC
        {limit_clause}
        """,
        values,
    )
    inserted = 0
    for row in rows or []:
        row_dict = dict(row)
        mirrored_at = datetime.now(timezone.utc).isoformat()
        category_meta = resolve_mirror_category_metadata(
            category=row_dict.get("mirrored_category"),
            product_type=row_dict.get("mirrored_product_type"),
            title=row_dict.get("title"),
        )
        # Phase O-1 followup: extract tags from seed_data so external
        # crawl data flows into the canonical tags column. Always writes
        # a list (possibly empty) to keep semantics consistent with
        # ingest_standard_products on Path A: empty = "we looked, no tags
        # found", NULL = "row predates the column".
        seed_tags = _extract_tags_from_seed_data(row_dict.get("seed_data"))
        # Phase O-2: derived taxonomy v1 for external-seed mirror rows.
        # Same pure-function call as Path A. price comes from the seed
        # row's price_amount; title + description + tags drive the
        # keyword extractors. mig 076.
        try:
            seed_price_value = float(row_dict.get("price_amount")) if row_dict.get("price_amount") is not None else None
        except (TypeError, ValueError):
            seed_price_value = None
        taxonomy = derive_taxonomy_v1(
            price=seed_price_value,
            title=row_dict.get("title"),
            description=row_dict.get("mirrored_description"),
            tags=seed_tags,
        )
        product_payload = {
            "external_seed": {
                "id": row_dict.get("id"),
                "external_product_id": row_dict.get("external_product_id"),
                "market": row_dict.get("market"),
                "tool": row_dict.get("tool"),
                "domain": row_dict.get("domain"),
                "destination_url": row_dict.get("destination_url"),
                "canonical_url": row_dict.get("canonical_url"),
                "price_amount": row_dict.get("price_amount"),
                "price_currency": row_dict.get("price_currency"),
                "availability": row_dict.get("availability"),
                "updated_at": row_dict.get("updated_at"),
            },
            "seed_data": row_dict.get("seed_data"),
            "mirror_meta": {
                "source_system": SOURCE_SYSTEM,
                "mirrored_at": mirrored_at,
                "duplicate_count": row_dict.get("duplicate_count"),
                "selection_rank": row_dict.get("rn"),
            },
        }
        freshness_json = {
            "mirrored_from": "external_product_seeds",
            "source_seed_id": row_dict.get("id"),
            "source_updated_at": row_dict.get("updated_at"),
            "mirrored_at": mirrored_at,
        }
        inserted_row = await database.fetch_one(
            """
            INSERT INTO catalog_products (
              product_key,
              merchant_id,
              platform,
              source_product_id,
              catalog_track,
              truth_tier,
              readiness_tier,
              source_system,
              source_ref,
              title,
              description,
              brand,
              product_type,
              category,
              category_path,
              category_confidence,
              category_label_source,
              canonical_url,
              image_url,
              product_payload,
              freshness_json,
              tags,
              price_tier,
              use_case_tags,
              lifestyle_tags,
              demographic,
              created_at,
              updated_at
            )
            VALUES (
              :product_key,
              :merchant_id,
              :platform,
              :source_product_id,
              :catalog_track,
              :truth_tier,
              :readiness_tier,
              :source_system,
              :source_ref,
              :title,
              :description,
              :brand,
              :product_type,
              :category,
              :category_path,
              :category_confidence,
              :category_label_source,
              :canonical_url,
              :image_url,
              CAST(:product_payload AS jsonb),
              CAST(:freshness_json AS jsonb),
              CAST(:tags AS jsonb),
              :price_tier,
              CAST(:use_case_tags AS jsonb),
              CAST(:lifestyle_tags AS jsonb),
              :demographic,
              now(),
              now()
            )
            ON CONFLICT (merchant_id, platform, source_product_id) DO NOTHING
            RETURNING product_key
            """,
            {
                "product_key": row_dict.get("product_key"),
                "merchant_id": MERCHANT_ID,
                "platform": PLATFORM,
                "source_product_id": row_dict.get("external_product_id"),
                "catalog_track": CATALOG_TRACK,
                "truth_tier": TRUTH_TIER,
                "readiness_tier": READINESS_TIER,
                "source_system": SOURCE_SYSTEM,
                "source_ref": row_dict.get("id"),
                "title": row_dict.get("title"),
                "description": row_dict.get("mirrored_description"),
                "brand": row_dict.get("mirrored_brand"),
                "product_type": row_dict.get("mirrored_product_type"),
                "category": row_dict.get("mirrored_category"),
                "category_path": category_meta.get("category_path"),
                "category_confidence": category_meta.get("category_confidence"),
                "category_label_source": category_meta.get("category_label_source"),
                "canonical_url": row_dict.get("destination_url"),
                "image_url": row_dict.get("image_url"),
                "product_payload": json.dumps(product_payload, ensure_ascii=False, default=_json_default),
                "freshness_json": json.dumps(freshness_json, ensure_ascii=False, default=_json_default),
                "tags": json.dumps(seed_tags, ensure_ascii=False),
                "price_tier": taxonomy["price_tier"],
                "use_case_tags": json.dumps(taxonomy["use_case_tags"], ensure_ascii=False),
                "lifestyle_tags": json.dumps(taxonomy["lifestyle_tags"], ensure_ascii=False),
                "demographic": taxonomy["demographic"],
            },
        )
        if inserted_row:
            inserted += 1
    return inserted


def _render_markdown(report: Dict[str, Any]) -> str:
    totals = report.get("totals") or {}
    lines = [
        "# External Seeds → Catalog Products Mirror",
        "",
        f"- ok: `{report.get('ok')}`",
        f"- apply: `{report.get('apply')}`",
        f"- limit: `{report.get('limit')}`",
        "",
        "## Totals",
        "",
        "| Metric | Count |",
        "| --- | ---: |",
    ]
    for key in [
        "external_total",
        "external_active",
        "active_standalone",
        "active_standalone_missing_external_product_id",
        "duplicate_active_standalone_rows",
        "duplicate_active_standalone_groups",
        "skipped_source_product_id_too_long",
        "skipped_product_key_too_long",
        "skipped_missing_title",
        "deduped_valid_candidates",
        "candidates_with_image",
        "candidates_with_description_50",
        "candidates_visible_quality_ready",
        "missing_catalog_products",
        "catalog_products_total",
        "catalog_products_with_sig",
        "catalog_products_external_seed",
        "catalog_products_external_seed_with_sig",
        "legacy_external_seed_catalog_rows",
        "catalog_products_visible_quality_with_sig",
        "inserted_catalog_products",
        "post_apply_missing_catalog_products",
        "post_apply_catalog_products_total",
        "post_apply_catalog_products_with_sig",
        "post_apply_catalog_products_external_seed",
        "post_apply_catalog_products_external_seed_with_sig",
        "post_apply_legacy_external_seed_catalog_rows",
        "post_apply_catalog_products_visible_quality_with_sig",
    ]:
        if key in totals:
            lines.append(f"| `{key}` | {totals[key]} |")
    lines.append("")
    lines.append("## Candidate By Market")
    lines.append("")
    lines.append("| Market | Rows |")
    lines.append("| --- | ---: |")
    for row in report.get("candidate_by_market") or []:
        lines.append(f"| {row.get('market') or 'unknown'} | {row.get('rows')} |")
    lines.append("")
    lines.append("## Top Candidate Domains")
    lines.append("")
    lines.append("| Domain | Rows |")
    lines.append("| --- | ---: |")
    for row in report.get("candidate_by_domain") or []:
        lines.append(f"| {row.get('domain') or 'unknown'} | {row.get('rows')} |")
    lines.append("")
    return "\n".join(lines) + "\n"


async def _run(args: argparse.Namespace) -> Dict[str, Any]:
    await database.connect()
    try:
        before = await _build_report(
            sample_limit=args.sample_limit,
            limit=args.limit,
            apply=args.apply,
        )
        if not before.get("ok"):
            return before

        report = before
        if args.apply:
            legacy_rows = int((before.get("totals") or {}).get("legacy_external_seed_catalog_rows") or 0)
            if legacy_rows > 0:
                before["ok"] = False
                before["error"] = (
                    "legacy external_seed catalog rows exist; clean or reconcile them before applying "
                    "the external_product_seeds mirror to avoid duplicate public PDPs"
                )
                return before
            inserted = await _apply(args.limit)
            after = await _build_report(
                sample_limit=args.sample_limit,
                limit=args.limit,
                apply=args.apply,
            )
            before_totals = before.get("totals") or {}
            after_totals = after.get("totals") or {}
            report = after
            report["before_totals"] = before_totals
            report["totals"] = {
                **before_totals,
                "inserted_catalog_products": inserted,
                "post_apply_missing_catalog_products": after_totals.get("missing_catalog_products"),
                "post_apply_catalog_products_total": after_totals.get("catalog_products_total"),
                "post_apply_catalog_products_with_sig": after_totals.get("catalog_products_with_sig"),
                "post_apply_catalog_products_external_seed": after_totals.get("catalog_products_external_seed"),
                "post_apply_catalog_products_external_seed_with_sig": after_totals.get("catalog_products_external_seed_with_sig"),
                "post_apply_legacy_external_seed_catalog_rows": after_totals.get("legacy_external_seed_catalog_rows"),
                "post_apply_catalog_products_visible_quality_with_sig": after_totals.get("catalog_products_visible_quality_with_sig"),
            }
        return report
    finally:
        await database.disconnect()


def main() -> int:
    args = _parse_args()
    report = asyncio.run(_run(args))
    json_blob = json.dumps(report, indent=2, ensure_ascii=False, default=_json_default)
    _write_if_requested(args.output_json, json_blob + "\n")
    _write_if_requested(args.output_md, _render_markdown(report))
    print(json_blob)
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
