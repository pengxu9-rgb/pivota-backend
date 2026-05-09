#!/usr/bin/env python3
"""Heal Path B (external_seed mirror) rows missing their canonical chain.

Phase 7d's mirror script writes catalog_skus + catalog_offers + the
synthetic catalog_merchants row on every NEW mirror, but the existing
~3,949 Path B rows in prod were written before 7d landed and have no
chain. The mirror's --apply path only processes "missing"
catalog_products rows (i.e., new external seeds) so it can't heal them.

This script does the heal: select Path B catalog_products rows with no
catalog_offers row, JOIN external_product_seeds for price/currency/
availability/destination_url, then call the same chain helpers
(_upsert_canonical_sku_for_mirror_row, _upsert_canonical_offer_for_mirror_row)
the mirror uses on new writes. Idempotent — re-runs are safe.

Usage:
  # Dry-run: show histogram of rows that would be healed.
  python3 scripts/backfill_canonical_chain_for_path_b_mirror.py --limit 0

  # Apply against a small slice first.
  python3 scripts/backfill_canonical_chain_for_path_b_mirror.py --apply --limit 50

  # Heal everything.
  python3 scripts/backfill_canonical_chain_for_path_b_mirror.py --apply --limit 0
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db.database import database  # noqa: E402
from scripts.mirror_external_seeds_to_catalog_products import (  # noqa: E402
    MERCHANT_ID,
    _ensure_external_seed_merchant,
    _upsert_canonical_offer_for_mirror_row,
    _upsert_canonical_sku_for_mirror_row,
)


logger = logging.getLogger("backfill_canonical_chain_for_path_b_mirror")


PROGRESS_LOG_EVERY = 250


# Pick the most-recently-updated external_product_seeds row per
# catalog_products row (DISTINCT ON). catalog_products.source_product_id
# corresponds to external_product_seeds.external_product_id for Path B
# (it's the identity tuple the mirror uses).
_BASE_SELECT = """
    SELECT DISTINCT ON (cp.product_key)
      cp.product_key,
      cp.source_product_id AS external_product_id,
      cp.title,
      cp.image_url,
      eps.id AS seed_id,
      eps.price_amount,
      eps.price_currency,
      eps.availability,
      eps.destination_url,
      eps.canonical_url,
      eps.domain,
      eps.market
    FROM catalog_products cp
    LEFT JOIN catalog_offers o ON o.product_key = cp.product_key
    LEFT JOIN external_product_seeds eps
      ON eps.external_product_id = cp.source_product_id
    WHERE cp.merchant_id = :merchant_id
      AND o.offer_id IS NULL
    ORDER BY cp.product_key, eps.updated_at DESC NULLS LAST
"""

SELECT_SQL = _BASE_SELECT + "\n    LIMIT :limit\n"
SELECT_SQL_NO_LIMIT = _BASE_SELECT + "\n"


def _build_row_dict_for_chain(row: Dict[str, Any]) -> Dict[str, Any]:
    """Reshape the joined row into the dict shape the chain helpers
    expect. The chain helpers were authored for the mirror script's
    in-memory row dict (which uses keys like `id`, `external_product_id`,
    `price_amount`, etc.); this adapter keeps the helpers untouched
    and reusable across both the live mirror path and this backfill."""
    return {
        "id": row.get("seed_id"),
        "external_product_id": row.get("external_product_id"),
        "title": row.get("title"),
        "image_url": row.get("image_url"),
        "price_amount": row.get("price_amount"),
        "price_currency": row.get("price_currency"),
        "availability": row.get("availability"),
        "destination_url": row.get("destination_url"),
        "canonical_url": row.get("canonical_url"),
        "domain": row.get("domain"),
        "market": row.get("market"),
    }


async def _fetch_candidates(limit: int) -> List[Dict[str, Any]]:
    if limit > 0:
        rows = await database.fetch_all(
            SELECT_SQL, {"merchant_id": MERCHANT_ID, "limit": int(limit)}
        )
    else:
        rows = await database.fetch_all(
            SELECT_SQL_NO_LIMIT, {"merchant_id": MERCHANT_ID}
        )
    return [dict(r) for r in rows or []]


async def _drive(args: argparse.Namespace) -> Dict[str, Any]:
    candidates = await _fetch_candidates(args.limit)
    logger.info(
        "fetched %d Path B rows missing canonical chain (limit=%d, apply=%s)",
        len(candidates),
        args.limit,
        args.apply,
    )

    has_seed_data = sum(1 for r in candidates if r.get("seed_id"))
    has_price = sum(1 for r in candidates if r.get("price_amount") is not None)
    by_market: Counter = Counter()
    by_domain: Counter = Counter()
    sample_rows: List[Dict[str, Any]] = []

    for r in candidates:
        by_market[r.get("market") or "unknown"] += 1
        by_domain[r.get("domain") or "unknown"] += 1
        if len(sample_rows) < 12:
            sample_rows.append({
                "product_key": r.get("product_key"),
                "external_product_id": r.get("external_product_id"),
                "title": r.get("title"),
                "price_amount": float(r["price_amount"]) if r.get("price_amount") is not None else None,
                "price_currency": r.get("price_currency"),
                "domain": r.get("domain"),
                "has_seed_row": r.get("seed_id") is not None,
            })

    applied = 0
    chain_failures = 0
    if args.apply:
        # Singleton merchant first — chain inserts are no-ops if the
        # synthetic merchant doesn't exist yet (FK from catalog_offers).
        await _ensure_external_seed_merchant()
        for idx, row in enumerate(candidates, start=1):
            product_key = row.get("product_key")
            if not product_key:
                continue
            chain_input = _build_row_dict_for_chain(row)
            try:
                await _upsert_canonical_sku_for_mirror_row(product_key, chain_input)
                await _upsert_canonical_offer_for_mirror_row(product_key, chain_input)
                applied += 1
            except Exception as exc:
                chain_failures += 1
                logger.warning(
                    "chain heal failed for product_key=%s: %r", product_key, exc
                )

            if idx % PROGRESS_LOG_EVERY == 0:
                logger.info(
                    "progress %d/%d (applied=%d, failures=%d)",
                    idx, len(candidates), applied, chain_failures,
                )

    return {
        "limit": args.limit,
        "apply": args.apply,
        "candidate_count": len(candidates),
        "applied_count": applied,
        "chain_failures": chain_failures,
        "rows_with_seed_data": has_seed_data,
        "rows_with_price": has_price,
        "by_market": dict(by_market.most_common(10)),
        "by_domain": dict(by_domain.most_common(15)),
        "sample_rows": sample_rows,
    }


def _print_summary(report: Dict[str, Any]) -> None:
    print()
    print("=== Path B canonical-chain backfill ===")
    print(f"  apply:              {report.get('apply')}")
    print(f"  limit:              {report.get('limit')}")
    print(f"  candidate count:    {report.get('candidate_count')}")
    print(f"  rows with seed row: {report.get('rows_with_seed_data')}")
    print(f"  rows with price:    {report.get('rows_with_price')}")
    if report.get("apply"):
        print(f"  applied count:      {report.get('applied_count')}")
        print(f"  chain failures:     {report.get('chain_failures')}")
    by_market = report.get("by_market") or {}
    if by_market:
        print("  by market:")
        for market, count in by_market.items():
            print(f"    {market:20s} {count}")
    by_domain = report.get("by_domain") or {}
    if by_domain:
        print("  top domains:")
        for domain, count in by_domain.items():
            print(f"    {domain:36s} {count}")


async def _connect_with_retry(*, attempts: int = 3, backoff_s: tuple = (5.0, 15.0, 30.0)) -> None:
    if getattr(database, "is_connected", False):
        return
    last_exc: Optional[Exception] = None
    for i in range(attempts):
        try:
            await database.connect()
            return
        except (asyncio.TimeoutError, OSError, ConnectionError) as exc:
            last_exc = exc
            if i + 1 < attempts:
                wait = backoff_s[i] if i < len(backoff_s) else backoff_s[-1]
                logger.warning(
                    "DB connect attempt %d/%d failed (%s); retrying in %.0fs",
                    i + 1, attempts, type(exc).__name__, wait,
                )
                await asyncio.sleep(wait)
            else:
                raise
    if last_exc is not None:
        raise last_exc


async def _run(args: argparse.Namespace) -> int:
    await _connect_with_retry()
    try:
        report = await _drive(args)
    finally:
        try:
            await database.disconnect()
        except Exception:
            pass
    _print_summary(report)
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually write skus + offers (default dry-run)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="max rows to process (0 = all). Default 0 because the heal "
        "is a one-shot — keeping a small cap defeats the purpose. "
        "Pass --limit 50 for a smoke test before the full run.",
    )
    return parser.parse_args()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
