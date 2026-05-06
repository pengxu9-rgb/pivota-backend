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


async def _validate_with_concurrency(
    candidates: List[Dict[str, Any]],
    *,
    concurrency: int,
    timeout_s: float,
) -> List[Dict[str, Any]]:
    sem = asyncio.Semaphore(max(1, concurrency))

    async def _one(cand: Dict[str, Any]) -> Dict[str, Any]:
        async with sem:
            try:
                return await validate_candidate(cand, timeout_s=timeout_s)
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
    )

    validated_count = sum(1 for r in results if r.get("offers"))
    rejected_count = len(results) - validated_count
    _write_jsonl(out_path, results)
    logger.info(
        "wrote %s rows to %s (validated=%s rejected=%s)",
        len(results),
        out_path,
        validated_count,
        rejected_count,
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
    pdps, seeds, skipped = ingest_validated_jsonl(rows, source_jsonl=str(in_path))
    logger.info(
        "ingest plan: pdps=%s seeds=%s skipped=%s",
        len(pdps),
        len(seeds),
        skipped,
    )
    if not args.apply:
        # Dry run — print one example row of each kind for inspection.
        if pdps:
            logger.info("sample pdp row: %s", json.dumps({k: v for k, v in pdps[0].items() if k != "product_payload"}, ensure_ascii=False))
        if seeds:
            logger.info("sample seed row: %s", json.dumps({k: v for k, v in seeds[0].items() if k != "seed_data"}, ensure_ascii=False))
        return 0

    # Apply mode — actually INSERT.
    from db.database import database  # noqa: E402

    if not getattr(database, "is_connected", False):
        await database.connect()

    inserted_pdps = 0
    inserted_seeds = 0
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
            inserted_pdps += 1
        except Exception as exc:
            logger.exception("insert pdp failed for product_key=%s — %s", pdp.get("product_key"), exc)

    for seed in seeds:
        try:
            await database.execute(
                """
                INSERT INTO external_product_seeds
                  (id, external_product_id, market, tool, title, image_url,
                   price_amount, price_currency, destination_url,
                   canonical_url, domain, attached_product_key, status,
                   seed_data)
                VALUES
                  (:id, :external_product_id, :market, :tool, :title, :image_url,
                   :price_amount, :price_currency, :destination_url,
                   :canonical_url, :domain, :attached_product_key, :status,
                   CAST(:seed_data AS jsonb))
                ON CONFLICT (id) DO UPDATE SET
                  external_product_id = EXCLUDED.external_product_id,
                  attached_product_key = EXCLUDED.attached_product_key,
                  destination_url = EXCLUDED.destination_url,
                  canonical_url = EXCLUDED.canonical_url,
                  image_url = EXCLUDED.image_url,
                  price_amount = EXCLUDED.price_amount,
                  status = EXCLUDED.status,
                  seed_data = EXCLUDED.seed_data,
                  updated_at = NOW()
                """,
                seed,
            )
            inserted_seeds += 1
        except Exception as exc:
            logger.exception("insert seed failed for id=%s — %s", seed.get("id"), exc)

    logger.info(
        "applied: inserted_pdps=%s inserted_seeds=%s",
        inserted_pdps,
        inserted_seeds,
    )
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
