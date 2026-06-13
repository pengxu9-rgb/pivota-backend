"""Backfill durable category_kind by re-deriving it over existing fields.

Re-runs services.category_kind.resolve_category_kind on catalog_products rows
whose category_kind is NULL and UPDATEs the ones that now resolve to a kind --
recovering rows the resolver newly classifies (exact subcategory-less beauty
paths, explicit supplement paths, ingestible-text supplements like Ownist).

FILL-ONLY: only touches category_kind IS NULL rows, and only writes when the
re-derive yields a non-null kind. Never overwrites an existing kind. Idempotent.
--dry-run reports the would-classify breakdown without writing.

Usage:
  python -m scripts.backfill_category_kind --dry-run
  python -m scripts.backfill_category_kind --limit 10000
"""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any, Dict

from db.database import database
from services.category_kind import resolve_category_kind


async def _connect_if_needed(db: Any) -> bool:
    was = bool(getattr(db, "is_connected", False))
    if not was and callable(getattr(db, "connect", None)):
        await db.connect()
    return was


async def _disconnect_if_needed(db: Any, was: bool) -> None:
    if not was and bool(getattr(db, "is_connected", False)) and callable(getattr(db, "disconnect", None)):
        await db.disconnect()


async def _drive(args: argparse.Namespace, *, db: Any = database) -> Dict[str, Any]:
    was = await _connect_if_needed(db)
    try:
        rows = await db.fetch_all(
            """
            SELECT product_key, category_path, product_type, title, tags
            FROM catalog_products
            WHERE category_kind IS NULL
            ORDER BY product_key
            LIMIT :lim
            """,
            {"lim": args.limit},
        )
        counts = {"skincare": 0, "haircare": 0, "supplement": 0}
        scanned = 0
        for row in rows:
            d = dict(row)
            scanned += 1
            kind = resolve_category_kind(
                d.get("category_path"), d.get("product_type"), d.get("title"), d.get("tags")
            )
            if kind not in counts:
                continue
            counts[kind] += 1
            if not args.dry_run:
                await db.execute(
                    """
                    UPDATE catalog_products
                    SET category_kind = :kind
                    WHERE product_key = :pk AND category_kind IS NULL
                    """,
                    {"kind": kind, "pk": d["product_key"]},
                )
    finally:
        await _disconnect_if_needed(db, was)
    return {
        "dry_run": args.dry_run,
        "scanned_null_kind": scanned,
        "classified": counts,
        "total_classified": sum(counts.values()),
    }


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--limit", type=int, default=20000)
    p.add_argument("--dry-run", action="store_true")
    return p.parse_args()


def main() -> int:
    print(json.dumps(asyncio.run(_drive(_parse_args())), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
