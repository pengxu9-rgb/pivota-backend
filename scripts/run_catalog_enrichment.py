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
  {"pdp": {brand, product_name, category_path, attribute_summary,
           tags?},
   "offers": [{merchant_inferred, canonical_url, destination_url,
               image_url, price, in_stock, validated_at, ...}]}

  pdp.tags is optional (Phase O-1 followup). Either a list of strings or
  a comma-separated string. Persisted to catalog_products.tags. Empty /
  missing is fine — write [] explicitly to signal "no tags" vs leaving
  it absent for the runner to default. Future Phase O-3 LabelAgent will
  fill this for rows where the curator didn't supply tags.

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
    plan = ingest_validated_jsonl(rows, source_jsonl=str(in_path))
    pdps = plan["pdps"]
    skus = plan["skus"]
    merchants = plan["merchants"]
    offers = plan["offers"]
    seeds = plan["seeds"]
    skipped = plan["skipped"]
    audit_reasons = plan.get("audit_reasons") or {}
    logger.info(
        "ingest plan: pdps=%s skus=%s merchants=%s offers=%s seeds=%s skipped=%s audit_reasons=%s",
        len(pdps), len(skus), len(merchants), len(offers), len(seeds), skipped, audit_reasons,
    )
    if not args.apply:
        if pdps:
            logger.info("sample pdp row: %s", json.dumps({k: v for k, v in pdps[0].items() if k != "product_payload"}, ensure_ascii=False, default=str))
        if skus:
            logger.info("sample sku row: %s", json.dumps({k: v for k, v in skus[0].items() if k != "sku_payload"}, ensure_ascii=False, default=str))
        if merchants:
            logger.info("sample merchant row: %s", json.dumps({k: v for k, v in merchants[0].items() if k != "metadata_json"}, ensure_ascii=False, default=str))
        if offers:
            logger.info("sample offer row: %s", json.dumps({k: v for k, v in offers[0].items() if k != "offer_payload"}, ensure_ascii=False, default=str))
        if seeds:
            logger.info("sample seed row: %s", json.dumps({k: v for k, v in seeds[0].items() if k != "seed_data"}, ensure_ascii=False, default=str))
        return 0

    # Apply mode — execute the plan via the shared FK-order executor
    # (services.catalog_enrichment_agent.apply) so the CLI + the programmatic
    # runner share one upsert code path (no SQL drift).
    from services.catalog_enrichment_agent.apply import apply_ingest_plan  # noqa: E402

    await apply_ingest_plan(
        plan,
        batch_label=f"run_catalog_enrichment:{args.category}:{in_path}",
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
