#!/usr/bin/env python3
"""Phase O-5b backfill — populate catalog_products.material / care /
size_guide using services.fashion_field_extractor on rows where those
columns are NULL.

For each catalog_products row missing all three fashion fields:
  - Read description + product_payload + seed_data JSONB to assemble a
    text haystack the regex extractor can run on.
  - Call extract_material / extract_care / extract_size_guide.
  - UPDATE only the fields that were extracted; leave others NULL.

Runs in batches of 1000. Idempotent: re-running only touches rows where
the target field is still NULL.

Designed to be safe to run on the whole catalog: the extractor is pure
Python + regex (no network, no LLM cost). When the future LLM extractor
ships, this script can swap in `extract_*` calls that route to
services/llm_providers/orchestrator.py — the substring grounding and
provenance shape stay identical.

Usage:
  python scripts/backfill_fashion_fields.py [--limit N] [--dry-run]
                                            [--category-prefix beauty/...]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db.database import database
from services.fashion_field_extractor import (  # noqa: E402
    ExtractionResult,
    extract_care,
    extract_material,
    extract_size_guide,
)

logger = logging.getLogger("backfill_fashion_fields")


def _description_haystack(row: Dict[str, Any]) -> str:
    """Assemble a text haystack from a catalog_products row.

    Sources, in priority order:
      1. description column (TEXT)
      2. product_payload.description / description_text (Shopify-style)
      3. product_payload.body_html (Shopify-style raw HTML)
      4. seed_data.snapshot.description / snapshot.body_html (external seeds)
      5. title (always last — least specific)
    """
    parts: List[str] = []
    if row.get("description"):
        parts.append(str(row["description"]))
    payload = row.get("product_payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            payload = None
    if isinstance(payload, dict):
        for key in ("description", "description_text", "body_html"):
            v = payload.get(key)
            if v and isinstance(v, str):
                parts.append(v)
    seed = row.get("seed_data")
    if isinstance(seed, str):
        try:
            seed = json.loads(seed)
        except Exception:
            seed = None
    if isinstance(seed, dict):
        snap = seed.get("snapshot") if isinstance(seed.get("snapshot"), dict) else None
        if isinstance(snap, dict):
            for key in ("description", "description_text", "body_html"):
                v = snap.get(key)
                if v and isinstance(v, str):
                    parts.append(v)
    if row.get("title"):
        parts.append(str(row["title"]))
    return "\n".join(parts)


async def _fetch_batch(
    *, limit: int, after_key: Optional[str], category_prefix: Optional[str],
) -> List[dict]:
    rows = await database.fetch_all(
        """
        SELECT
          cp.product_key,
          cp.pivota_signature_id,
          cp.title,
          cp.description,
          cp.category_path,
          cp.product_payload,
          eps.seed_data
        FROM catalog_products cp
        LEFT JOIN external_product_seeds eps
          ON eps.attached_product_key = cp.product_key
        WHERE cp.material IS NULL
          AND cp.care IS NULL
          AND cp.size_guide IS NULL
          AND (CAST(:after_key AS text) IS NULL OR cp.product_key > CAST(:after_key AS text))
          AND (
            CAST(:category_prefix AS text) IS NULL
            OR cp.category_path LIKE CAST(:category_prefix AS text) || '%'
          )
        ORDER BY cp.product_key ASC
        LIMIT :limit
        """,
        {
            "limit": limit,
            "after_key": after_key,
            "category_prefix": category_prefix,
        },
    )
    return [dict(row) for row in rows or []]


async def _apply_update(
    *,
    product_key: str,
    material: Optional[ExtractionResult],
    care: Optional[ExtractionResult],
    size_guide: Optional[ExtractionResult],
) -> None:
    set_clauses: List[str] = []
    where_clauses: List[str] = ["product_key = :key"]
    params: Dict[str, Any] = {"key": product_key}
    if material and material.value:
        set_clauses += [
            "material = :material",
            "material_source = :material_source",
            "material_confidence = :material_confidence",
        ]
        params["material"] = material.value
        params["material_source"] = material.source
        params["material_confidence"] = material.confidence
        where_clauses.append("material IS NULL")
    if care and care.value:
        set_clauses += [
            "care = :care",
            "care_source = :care_source",
            "care_confidence = :care_confidence",
        ]
        params["care"] = care.value
        params["care_source"] = care.source
        params["care_confidence"] = care.confidence
        where_clauses.append("care IS NULL")
    if size_guide and size_guide.value:
        set_clauses += [
            "size_guide = :size_guide",
            "size_guide_source = :size_guide_source",
            "size_guide_confidence = :size_guide_confidence",
        ]
        # size_guide column is JSONB; the regex extractor returns a string
        # so we wrap it in a simple {raw: ...} envelope. Future LLM
        # extractor will return a structured {columns, rows, ...} dict
        # directly.
        params["size_guide"] = json.dumps({"raw": size_guide.value})
        params["size_guide_source"] = size_guide.source
        params["size_guide_confidence"] = size_guide.confidence
        where_clauses.append("size_guide IS NULL")
    if not set_clauses:
        return
    sql = (
        "UPDATE catalog_products SET "
        + ", ".join(set_clauses)
        + " WHERE "
        + " AND ".join(where_clauses)
    )
    await database.execute(sql, params)


async def run_fashion_backfill(
    *,
    batch_size: int = 1000,
    limit: int = 0,
    dry_run: bool = False,
    category_prefix: Optional[str] = None,
    sample_limit: int = 20,
) -> Dict[str, Any]:
    if not getattr(database, "is_connected", False):
        await database.connect()

    total = 0
    material_hits = 0
    care_hits = 0
    size_guide_hits = 0
    full_misses = 0
    after_key: Optional[str] = None
    samples: List[Dict[str, Any]] = []

    while True:
        remaining = max(0, int(limit or 0) - total) if limit else 0
        if limit and remaining <= 0:
            break
        rows = await _fetch_batch(
            limit=min(batch_size, remaining) if limit else batch_size,
            after_key=after_key,
            category_prefix=category_prefix,
        )
        if not rows:
            break
        for row in rows:
            total += 1
            after_key = str(row.get("product_key") or "")
            haystack = _description_haystack(row)
            # Phase O-5b v2: extractors are async + category-gated. They
            # short-circuit immediately for non-fashion rows, so iterating
            # the whole catalog stays cheap even at the global scope.
            row_title = row.get("title")
            row_category_path = row.get("category_path")
            material = await extract_material(
                title=row_title, description=haystack, category_path=row_category_path,
            )
            care = await extract_care(
                title=row_title, description=haystack, category_path=row_category_path,
            )
            size_guide = await extract_size_guide(
                title=row_title, description=haystack, category_path=row_category_path,
            )
            any_hit = bool(material.value or care.value or size_guide.value)
            if any_hit:
                if material.value:
                    material_hits += 1
                if care.value:
                    care_hits += 1
                if size_guide.value:
                    size_guide_hits += 1
                if not dry_run:
                    await _apply_update(
                        product_key=row["product_key"],
                        material=material,
                        care=care,
                        size_guide=size_guide,
                    )
                if len(samples) < sample_limit:
                    samples.append({
                        "product_key": row.get("product_key"),
                        "pivota_signature_id": row.get("pivota_signature_id"),
                        "title": row.get("title"),
                        "category_path": row.get("category_path"),
                        "material": material.value,
                        "material_confidence": material.confidence if material.value else None,
                        "care": care.value,
                        "care_confidence": care.confidence if care.value else None,
                        "size_guide": size_guide.value,
                        "size_guide_confidence": size_guide.confidence if size_guide.value else None,
                    })
            else:
                full_misses += 1
            if total % 100 == 0:
                logger.info(
                    "total=%d material=%d care=%d size_guide=%d full_misses=%d",
                    total, material_hits, care_hits, size_guide_hits, full_misses,
                )

    return {
        "total": total,
        "material_hits": material_hits,
        "care_hits": care_hits,
        "size_guide_hits": size_guide_hits,
        "full_misses": full_misses,
        "dry_run": dry_run,
        "batch_size": batch_size,
        "limit": limit,
        "category_prefix": category_prefix,
        "samples": samples,
    }


async def _run(args: argparse.Namespace) -> int:
    report = await run_fashion_backfill(
        batch_size=args.batch_size,
        limit=args.limit,
        dry_run=args.dry_run,
        category_prefix=args.category_prefix,
        sample_limit=args.sample_limit,
    )
    logger.info(
        "Backfill complete: total=%d material=%d care=%d size_guide=%d dry_run=%s",
        report["total"],
        report["material_hits"],
        report["care_hits"],
        report["size_guide_hits"],
        report["dry_run"],
    )
    print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument("--limit", type=int, default=0, help="cap total rows; 0 = no cap")
    parser.add_argument("--sample-limit", type=int, default=20)
    parser.add_argument("--dry-run", action="store_true", help="don't UPDATE; just count")
    parser.add_argument(
        "--category-prefix",
        default=None,
        help="restrict to rows whose category_path starts with this prefix "
             "(e.g. 'fashion/apparel' once the classifier has fashion patterns)",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
