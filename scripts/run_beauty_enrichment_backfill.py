"""Backfill deterministic beauty enrichment over existing catalog products.

Fills empty concerns / active_ingredients on shell SKUs from their raw content
(ownership-safe, idempotent) and recomputes serving eligibility. Use --dry-run
first to see the lift without writing.

Usage:
  python -m scripts.run_beauty_enrichment_backfill --product-key pk1 --dry-run
  python -m scripts.run_beauty_enrichment_backfill --merchant merch_xxx --limit 200
  python -m scripts.run_beauty_enrichment_backfill --category skincare --limit 500
"""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any, Dict, List

from db.database import database
from services.beauty_enrichment_persist import enrich_and_persist_product


async def _connect_if_needed(db: Any) -> bool:
    was = bool(getattr(db, "is_connected", False))
    if not was and callable(getattr(db, "connect", None)):
        await db.connect()
    return was


async def _disconnect_if_needed(db: Any, was: bool) -> None:
    if not was and bool(getattr(db, "is_connected", False)) and callable(getattr(db, "disconnect", None)):
        await db.disconnect()


async def _resolve_keys(args: argparse.Namespace, *, db: Any) -> List[str]:
    keys = list(args.product_keys)
    if args.product_keys_file:
        with open(args.product_keys_file, "r", encoding="utf-8") as handle:
            keys.extend(l.strip() for l in handle if l.strip() and not l.startswith("#"))
    if args.merchant or args.category:
        conds = ["product_key IS NOT NULL"]
        params: Dict[str, Any] = {"lim": args.limit}
        if args.merchant:
            conds.append("merchant_id = :mid")
            params["mid"] = args.merchant
        if args.category:
            conds.append("category_kind = :ck")
            params["ck"] = args.category
        rows = await db.fetch_all(
            f"SELECT product_key FROM catalog_products WHERE {' AND '.join(conds)} "
            "ORDER BY product_key LIMIT :lim",
            params,
        )
        keys.extend(dict(r)["product_key"] for r in rows)
    # de-dupe, preserve order
    seen, out = set(), []
    for k in keys:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


async def _drive(args: argparse.Namespace, *, db: Any = database) -> Dict[str, Any]:
    was = await _connect_if_needed(db)
    try:
        keys = await _resolve_keys(args, db=db)
        results: List[Dict[str, Any]] = []
        for pk in keys:
            try:
                results.append(await enrich_and_persist_product(pk, db=db, dry_run=args.dry_run))
            except Exception as exc:  # noqa: BLE001 -- surface, don't abort the batch
                results.append({"product_key": pk, "status": "error", "error": str(exc)})
    finally:
        await _disconnect_if_needed(db, was)

    ok = [r for r in results if r.get("status") == "ok"]
    concerns_filled = sum(1 for r in ok if r["written"]["concerns"])
    actives_filled = sum(1 for r in ok if r["written"]["actives_skus"])
    became_servable = sum(1 for r in ok if r.get("serving_eligible") is True)
    return {
        "dry_run": args.dry_run,
        "n": len(results),
        "ok": len(ok),
        "errors": sum(1 for r in results if r.get("status") == "error"),
        "not_found": sum(1 for r in results if r.get("status") == "not_found"),
        "concerns_filled": concerns_filled,
        "actives_filled": actives_filled,
        "serving_eligible_after": became_servable,
        "sample": ok[:8],
    }


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--product-key", dest="product_keys", action="append", default=[])
    p.add_argument("--product-keys-file")
    p.add_argument("--merchant")
    p.add_argument("--category", choices=["skincare", "haircare", "supplement"])
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    if not (args.product_keys or args.product_keys_file or args.merchant or args.category):
        p.error("provide --product-key / --product-keys-file / --merchant / --category")
    return args


def main() -> int:
    report = asyncio.run(_drive(_parse_args()))
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
