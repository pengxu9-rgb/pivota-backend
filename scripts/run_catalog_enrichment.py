#!/usr/bin/env python3
"""Catalog enrichment runner — drives Stages 2 (Gemini URL validation)
and 3 (DB ingestion) for a given category.

Usage:
  # Stage 2 only — read candidates JSONL, write validated JSONL.
  GEMINI_API_KEY=... python scripts/run_catalog_enrichment.py validate \\
    --category lipstick

  # Stage 3 only — read validated JSONL, INSERT into DB.
  python scripts/run_catalog_enrichment.py ingest --category lipstick

  # Full pipeline.
  GEMINI_API_KEY=... python scripts/run_catalog_enrichment.py run \\
    --category lipstick

Validated JSONL format (one JSON per line):
  {"pdp": {brand, product_name, category_path, attribute_summary},
   "offers": [{merchant_inferred, canonical_url, destination_url,
               image_url, price, in_stock, validated_at, ...}]}

By default reads:
  data/catalog_enrichment/<category>_pdp_candidates.jsonl
  data/catalog_enrichment/<category>_validated.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.catalog_enrichment_agent.gemini_url_validator import (  # noqa: E402
    validate_candidate,
)
from services.catalog_enrichment_agent.ingestion import (  # noqa: E402
    ingest_validated_jsonl,
)

logger = logging.getLogger("run_catalog_enrichment")

DEFAULT_DATA_DIR = Path("data/catalog_enrichment")


def _candidates_path(category: str, data_dir: Path) -> Path:
    return data_dir / f"{category}_pdp_candidates.jsonl"


def _validated_path(category: str, data_dir: Path) -> Path:
    return data_dir / f"{category}_validated.jsonl"


def _validation_summary_path(category: str, data_dir: Path) -> Path:
    return data_dir / f"{category}_validation_summary.json"


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    out: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                out.append(json.loads(stripped))
            except json.JSONDecodeError as exc:
                logger.warning("skip invalid jsonl line %s in %s: %s", line_no, path, exc)
    return out


def _write_jsonl(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _build_validation_summary(
    *,
    category: str,
    candidates_input: int,
    results: List[Dict[str, Any]],
    output_path: Path,
) -> Dict[str, Any]:
    drop_counts: Counter = Counter()
    dropped: List[Dict[str, Any]] = []
    for row in results:
        offers = row.get("offers") or []
        if offers:
            continue
        reason = str(row.get("validation_drop_reason") or "unknown_drop")
        drop_counts[reason] += 1
        pdp = row.get("pdp") if isinstance(row.get("pdp"), dict) else {}
        dropped.append({
            "brand": pdp.get("brand"),
            "product_name": pdp.get("product_name"),
            "category_path": pdp.get("category_path"),
            "validation_drop_reason": reason,
            "validation_drop_detail": row.get("validation_drop_detail") or "",
            "validation_attempts": row.get("validation_attempts"),
            "validation_retried": row.get("validation_retried"),
        })
    validated = sum(1 for row in results if row.get("offers"))
    return {
        "category": category,
        "candidates_input": candidates_input,
        "validated_count": validated,
        "dropped_count": max(0, len(results) - validated),
        "drop_reason_counts": dict(sorted(drop_counts.items())),
        "output_path": str(output_path),
        "dropped": dropped,
    }


def _format_validation_summary(summary: Dict[str, Any]) -> str:
    lines = [
        "Validation summary:",
        f"  Candidates input: {summary.get('candidates_input', 0)}",
        f"  Validated (>=1 offer): {summary.get('validated_count', 0)}",
        f"  Dropped: {summary.get('dropped_count', 0)}",
    ]
    for reason, count in (summary.get("drop_reason_counts") or {}).items():
        lines.append(f"    {reason}: {count}")
    return "\n".join(lines)


async def _validate_with_concurrency(
    candidates: List[Dict[str, Any]],
    *,
    concurrency: int,
    timeout_s: float,
    max_retries: int,
) -> List[Dict[str, Any]]:
    sem = asyncio.Semaphore(max(1, concurrency))

    async def _one(cand: Dict[str, Any]) -> Dict[str, Any]:
        async with sem:
            try:
                return await validate_candidate(
                    cand,
                    timeout_s=timeout_s,
                    max_retries=max_retries,
                )
            except Exception as exc:
                logger.exception("validate failed for %s — %s", cand.get("product_name"), exc)
                return {
                    "pdp": {
                        "brand": cand.get("brand"),
                        "product_name": cand.get("product_name"),
                        "category_path": cand.get("category_path"),
                        "attribute_summary": cand.get("attribute_summary"),
                    },
                    "offers": [],
                    "validation_drop_reason": "validator_exception",
                    "validation_drop_detail": str(exc)[:500],
                    "validation_attempts": 1,
                    "validation_retried": False,
                }

    return await asyncio.gather(*(_one(c) for c in candidates))


async def _do_validate(args: argparse.Namespace) -> int:
    data_dir = Path(args.data_dir)
    in_path = _candidates_path(args.category, data_dir)
    out_path = _validated_path(args.category, data_dir)

    candidates = _read_jsonl(in_path)
    if not candidates:
        logger.error("no candidates in %s", in_path)
        return 1
    if args.limit:
        candidates = candidates[: args.limit]
    logger.info(
        "validating %s candidates from %s (concurrency=%s)",
        len(candidates),
        in_path,
        args.concurrency,
    )

    results = await _validate_with_concurrency(
        candidates,
        concurrency=args.concurrency,
        timeout_s=args.timeout,
        max_retries=args.max_retries,
    )

    validated_count = sum(1 for r in results if r.get("offers"))
    rejected_count = len(results) - validated_count
    _write_jsonl(out_path, results)
    summary = _build_validation_summary(
        category=args.category,
        candidates_input=len(candidates),
        results=results,
        output_path=out_path,
    )
    summary_path = _validation_summary_path(args.category, data_dir)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(_format_validation_summary(summary))
    logger.info(
        "wrote %s rows to %s (validated=%s rejected=%s summary=%s)",
        len(results),
        out_path,
        validated_count,
        rejected_count,
        summary_path,
    )
    return 0


async def _do_ingest(args: argparse.Namespace) -> int:
    """Read validated JSONL and emit the row dicts. With --apply, executes
    INSERT against the DB; without --apply, prints summary counts only."""
    data_dir = Path(args.data_dir)
    in_path = _validated_path(args.category, data_dir)
    rows = _read_jsonl(in_path)
    if not rows:
        logger.error("no validated rows in %s", in_path)
        return 1
    plan = ingest_validated_jsonl(rows, source_jsonl=str(in_path))
    pdps = plan["pdps"]
    skus = plan["skus"]
    merchants = plan["merchants"]
    offers = plan["offers"]
    seeds = plan["seeds"]
    skipped = plan["skipped"]
    logger.info(
        "ingest plan: pdps=%s skus=%s merchants=%s offers=%s seeds=%s skipped=%s",
        len(pdps), len(skus), len(merchants), len(offers), len(seeds), skipped,
    )
    if not args.apply:
        if pdps:
            logger.info("sample pdp row: %s", json.dumps({k: v for k, v in pdps[0].items() if k != "product_payload"}, ensure_ascii=False))
        if skus:
            logger.info("sample sku row: %s", json.dumps({k: v for k, v in skus[0].items() if k != "sku_payload"}, ensure_ascii=False))
        if merchants:
            logger.info("sample merchant row: %s", json.dumps({k: v for k, v in merchants[0].items() if k != "metadata_json"}, ensure_ascii=False))
        if offers:
            logger.info("sample offer row: %s", json.dumps({k: v for k, v in offers[0].items() if k != "offer_payload"}, ensure_ascii=False))
        if seeds:
            logger.info("sample seed row: %s", json.dumps({k: v for k, v in seeds[0].items() if k != "seed_data"}, ensure_ascii=False))
        return 0

    # Apply mode — INSERT in FK order:
    #   merchants → products → skus → offers → seeds
    from db.database import database  # noqa: E402

    if not getattr(database, "is_connected", False):
        await database.connect()

    counts = {"merchants": 0, "pdps": 0, "skus": 0, "offers": 0, "seeds": 0}

    # 1. catalog_merchants — UPSERT by merchant_id (FK target for offers).
    for merchant in merchants:
        try:
            await database.execute(
                """
                INSERT INTO catalog_merchants
                  (merchant_id, merchant_name, primary_platform, status,
                   source_system, source_ref, metadata_json)
                VALUES
                  (:merchant_id, :merchant_name, :primary_platform, :status,
                   :source_system, :source_ref, CAST(:metadata_json AS jsonb))
                ON CONFLICT (merchant_id) DO UPDATE SET
                  merchant_name = COALESCE(EXCLUDED.merchant_name, catalog_merchants.merchant_name),
                  primary_platform = COALESCE(EXCLUDED.primary_platform, catalog_merchants.primary_platform),
                  status = EXCLUDED.status,
                  source_ref = COALESCE(EXCLUDED.source_ref, catalog_merchants.source_ref),
                  metadata_json = EXCLUDED.metadata_json,
                  updated_at = NOW()
                """,
                merchant,
            )
            counts["merchants"] += 1
        except Exception as exc:
            logger.exception("insert merchant failed for merchant_id=%s — %s", merchant.get("merchant_id"), exc)

    # 2. catalog_products — UPSERT by product_key (already wired in Phase 4/6).
    for pdp in pdps:
        try:
            await database.execute(
                """
                INSERT INTO catalog_products
                  (product_key, merchant_id, platform, source_product_id,
                   catalog_track, truth_tier, readiness_tier, source_system,
                   title, description, brand, product_type, category,
                   category_path, category_confidence, category_label_source,
                   canonical_url, image_url, product_payload,
                   pdp_scope, pdp_scope_source, pdp_scope_set_at)
                VALUES
                  (:product_key, :merchant_id, :platform, :source_product_id,
                   :catalog_track, :truth_tier, :readiness_tier, :source_system,
                   :title, :description, :brand, :product_type, :category,
                   :category_path, :category_confidence, :category_label_source,
                   :canonical_url, :image_url, CAST(:product_payload AS jsonb),
                   :pdp_scope, :pdp_scope_source, NOW())
                ON CONFLICT (product_key) DO UPDATE SET
                  category_path = EXCLUDED.category_path,
                  category_confidence = EXCLUDED.category_confidence,
                  category_label_source = EXCLUDED.category_label_source,
                  canonical_url = EXCLUDED.canonical_url,
                  image_url = EXCLUDED.image_url,
                  product_payload = EXCLUDED.product_payload,
                  pdp_scope = EXCLUDED.pdp_scope,
                  pdp_scope_source = EXCLUDED.pdp_scope_source,
                  pdp_scope_set_at = NOW(),
                  updated_at = NOW()
                """,
                pdp,
            )
            counts["pdps"] += 1
        except Exception as exc:
            logger.exception("insert pdp failed for product_key=%s — %s", pdp.get("product_key"), exc)

    # 3. catalog_skus — INSERT one synthetic 'canonical' SKU per PDP.
    for sku in skus:
        try:
            await database.execute(
                """
                INSERT INTO catalog_skus
                  (sku_key, product_key, merchant_id, platform,
                   source_product_id, source_variant_id, sku, barcode,
                   title, currency, image_url,
                   visible_attributes, visible_option_labels, ingredient_ids,
                   sku_payload, readiness_tier)
                VALUES
                  (:sku_key, :product_key, :merchant_id, :platform,
                   :source_product_id, :source_variant_id, :sku, :barcode,
                   :title, :currency, :image_url,
                   CAST(:visible_attributes AS jsonb),
                   CAST(:visible_option_labels AS jsonb),
                   CAST(:ingredient_ids AS jsonb),
                   CAST(:sku_payload AS jsonb), :readiness_tier)
                ON CONFLICT (sku_key) DO UPDATE SET
                  title = EXCLUDED.title,
                  image_url = EXCLUDED.image_url,
                  sku_payload = EXCLUDED.sku_payload,
                  readiness_tier = EXCLUDED.readiness_tier,
                  updated_at = NOW()
                """,
                sku,
            )
            counts["skus"] += 1
        except Exception as exc:
            logger.exception("insert sku failed for sku_key=%s — %s", sku.get("sku_key"), exc)

    # 4. catalog_offers — INSERT one row per validated retailer offer.
    for offer in offers:
        try:
            await database.execute(
                """
                INSERT INTO catalog_offers
                  (offer_id, sku_key, product_key, merchant_id,
                   catalog_track, truth_tier, readiness_tier, offer_mode,
                   channel, availability, inventory_quantity, currency,
                   list_price, merchant_effective_price, estimated_best_price,
                   price_confidence, source_system, source_ref, offer_payload)
                VALUES
                  (:offer_id, :sku_key, :product_key, :merchant_id,
                   :catalog_track, :truth_tier, :readiness_tier, :offer_mode,
                   :channel, :availability, :inventory_quantity, :currency,
                   :list_price, :merchant_effective_price, :estimated_best_price,
                   :price_confidence, :source_system, :source_ref,
                   CAST(:offer_payload AS jsonb))
                ON CONFLICT (offer_id) DO UPDATE SET
                  availability = EXCLUDED.availability,
                  inventory_quantity = EXCLUDED.inventory_quantity,
                  list_price = EXCLUDED.list_price,
                  merchant_effective_price = EXCLUDED.merchant_effective_price,
                  estimated_best_price = EXCLUDED.estimated_best_price,
                  price_confidence = EXCLUDED.price_confidence,
                  offer_payload = EXCLUDED.offer_payload,
                  updated_at = NOW()
                """,
                offer,
            )
            counts["offers"] += 1
        except Exception as exc:
            logger.exception("insert offer failed for offer_id=%s — %s", offer.get("offer_id"), exc)

    # 5. external_product_seeds — audit + legacy compatibility.
    for seed in seeds:
        try:
            await database.execute(
                """
                INSERT INTO external_product_seeds
                  (id, external_product_id, market, tool, title, image_url,
                   price_amount, price_currency, destination_url,
                   canonical_url, domain, attached_product_key, status,
                   availability, seed_data)
                VALUES
                  (:id, :external_product_id, :market, :tool, :title, :image_url,
                   :price_amount, :price_currency, :destination_url,
                   :canonical_url, :domain, :attached_product_key, :status,
                   :availability, CAST(:seed_data AS jsonb))
                ON CONFLICT (id) DO UPDATE SET
                  external_product_id = EXCLUDED.external_product_id,
                  attached_product_key = EXCLUDED.attached_product_key,
                  destination_url = EXCLUDED.destination_url,
                  canonical_url = EXCLUDED.canonical_url,
                  image_url = EXCLUDED.image_url,
                  price_amount = EXCLUDED.price_amount,
                  status = EXCLUDED.status,
                  availability = EXCLUDED.availability,
                  seed_data = EXCLUDED.seed_data,
                  updated_at = NOW()
                """,
                seed,
            )
            counts["seeds"] += 1
        except Exception as exc:
            logger.exception("insert seed failed for id=%s — %s", seed.get("id"), exc)

    logger.info("applied: %s", counts)
    return 0


async def _do_run(args: argparse.Namespace) -> int:
    rc = await _do_validate(args)
    if rc != 0:
        return rc
    return await _do_ingest(args)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["validate", "ingest", "run"])
    parser.add_argument("--category", required=True, help="category token, e.g. lipstick")
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=45.0)
    parser.add_argument("--max-retries", type=int, default=1)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--apply", action="store_true",
                        help="ingest mode: actually INSERT (default is dry-run print-only)")
    return parser.parse_args()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args()
    if args.command == "validate":
        return asyncio.run(_do_validate(args))
    if args.command == "ingest":
        return asyncio.run(_do_ingest(args))
    return asyncio.run(_do_run(args))


if __name__ == "__main__":
    sys.exit(main())
