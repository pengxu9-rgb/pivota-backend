"""Source canonical INCI for products end-to-end: discover -> resolve -> ingest.

The full canonical-sourcing flow (ADR-001) per product: discover its
authoritative sources (brand-official PDP / barcode), resolve the INCI, and
ingest verified actives by source precedence. Run over one product, a merchant's
beauty catalog, or a category. --dry-run resolves without writing.

Usage:
  python -m scripts.source_canonical_inci --product-key pk --dry-run
  python -m scripts.source_canonical_inci --merchant merch_xxx --limit 100
  python -m scripts.source_canonical_inci --category skincare --limit 200
"""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any, Dict, List

from db.database import database
from services.canonical_source_discovery import source_canonical_inci


async def _resolve_keys(args: argparse.Namespace, *, db: Any) -> List[str]:
    if args.product_keys:
        return list(args.product_keys)
    conds = ["product_key IS NOT NULL"]
    params: Dict[str, Any] = {"lim": args.limit}
    if args.merchant:
        conds.append("merchant_id = :mid")
        params["mid"] = args.merchant
    if args.category:
        conds.append("category_kind = :ck")
        params["ck"] = args.category
    rows = await db.fetch_all(
        f"SELECT product_key FROM catalog_products WHERE {' AND '.join(conds)} ORDER BY product_key LIMIT :lim",
        params,
    )
    return [dict(r)["product_key"] for r in rows]


async def _drive(args: argparse.Namespace, *, db: Any = database) -> Dict[str, Any]:
    was = bool(getattr(db, "is_connected", False))
    if not was:
        await db.connect()
    try:
        keys = await _resolve_keys(args, db=db)
        results = []
        for pk in keys:
            try:
                results.append(await source_canonical_inci(pk, db=db, dry_run=args.dry_run))
            except Exception as exc:  # noqa: BLE001
                results.append({"product_key": pk, "status": "error", "error": str(exc)})
    finally:
        if not was:
            await db.disconnect()

    sourced = [r for r in results if r.get("status") == "ok"]
    by_source: Dict[str, int] = {}
    for r in sourced:
        s = (r.get("sourced_from") or {}).get("source", "?")
        by_source[s] = by_source.get(s, 0) + 1
    return {
        "dry_run": args.dry_run,
        "n": len(results),
        "sourced": len(sourced),
        "by_source": by_source,
        "no_inci_sourced": sum(1 for r in results if r.get("status") == "no_inci_sourced"),
        "no_candidates": sum(1 for r in results if r.get("status") == "no_candidates"),
        "errors": sum(1 for r in results if r.get("status") == "error"),
        "sample": sourced[:8],
    }


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--product-key", dest="product_keys", action="append", default=[])
    p.add_argument("--merchant")
    p.add_argument("--category", choices=["skincare", "haircare", "supplement"])
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    if not (args.product_keys or args.merchant or args.category):
        p.error("provide --product-key / --merchant / --category")
    return args


def main() -> int:
    print(json.dumps(asyncio.run(_drive(_parse_args())), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
