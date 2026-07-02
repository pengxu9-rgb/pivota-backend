"""Remediation: demote color-cosmetic (makeup) products that were mis-stamped
category_kind='skincare'/'haircare' (bypassing resolve_category_kind, which is
path-based). ~416 serving products are affected — a foundation surfacing as
"skincare, Contains Niacinamide" is a misleading agent result.

Sets category_kind=NULL for positively-detected makeup (services.category_kind
.is_makeup — conservative, excludes hybrids like BB cream / tinted moisturizer).
The "Contains X" evidence claims are ingredient-presence facts and stay valid;
only the wrong *category* is corrected, so the product no longer surfaces as
skincare. Idempotent; dry-run by default.

Usage:
  python3 scripts/remediate_makeup_category.py            # dry-run (count + sample)
  python3 scripts/remediate_makeup_category.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db.database import database  # noqa: E402
from services.category_kind import is_makeup  # noqa: E402


async def _fetch_candidates():
    return await database.fetch_all(
        """
        SELECT cp.product_key, cp.content_key, cp.brand, cp.title,
               cp.product_type, cp.category, cp.category_kind
        FROM catalog_products cp
        WHERE cp.category_kind IN ('skincare', 'haircare')
        """
    )


async def _drive(args: argparse.Namespace) -> None:
    if not getattr(database, "is_connected", False):
        await database.connect()
    try:
        rows = [dict(r) for r in await _fetch_candidates()]
        makeup = [
            r for r in rows
            if is_makeup(title=r["title"], product_type=r["product_type"], category=r["category"])
        ]
        print(f"{'APPLY' if args.apply else 'DRY'} :: category_kind skincare/haircare rows={len(rows)} "
              f"detected_makeup={len(makeup)}")
        if not args.apply:
            for r in makeup[:20]:
                print(f"  demote {r['category_kind']:9} -> NULL | {r['brand']} — {(r['title'] or '')[:50]}")
            return

        updated = 0
        content_keys = []
        for r in makeup:
            await database.execute(
                "UPDATE catalog_products SET category_kind=NULL, updated_at=NOW() WHERE product_key=:pk",
                {"pk": r["product_key"]},
            )
            updated += 1
            if r["content_key"]:
                content_keys.append(r["content_key"])
        print(f"demoted category_kind -> NULL for {updated} makeup products")
        print(f"affected content_keys: {len(set(content_keys))} "
              f"(serving/category filtering reads category_kind live; no apv rebuild needed — "
              f"evidence claims are category-agnostic)")
    finally:
        if getattr(database, "is_connected", False):
            await database.disconnect()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--apply", action="store_true", help="write (default: dry-run)")
    asyncio.run(_drive(p.parse_args()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
