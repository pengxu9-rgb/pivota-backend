"""Reap orphaned agent_pdp_view rows (content_key no longer in catalog_products).

agent_pdp_view is a content_key-keyed cache refreshed with UPSERT-on-content_key.
content_key = make_content_key(brand, title, gtin) is derived from mutable fields,
so when a product is re-keyed on re-sync (title/barcode change) catalog_products
moves to the new content_key in place and the OLD content_key's view row is left
behind — an orphan that squats on the still-live pivota_signature_id (a latent
/products/sig_* mis-serve, and it blocks the live row from materializing under the
unique sig index).

The inline reap in services.catalog_sync_service handles the common case at
re-key time; this script is the catch-all sweep for every OTHER re-key site and
the periodic backstop. Safe + idempotent: only deletes view rows whose content_key
has NO catalog_products row (NOT-EXISTS guard, re-checked at delete time).

Usage:
  python -m scripts.reap_orphaned_agent_pdp_view                 # dry-run (default)
  python -m scripts.reap_orphaned_agent_pdp_view --apply         # delete orphans
  python -m scripts.reap_orphaned_agent_pdp_view --apply --limit 500
"""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any, Dict

from db.database import database
from services.agent_pdp_view_assembler import reap_orphaned_agent_pdp_view_rows


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
        return await reap_orphaned_agent_pdp_view_rows(
            db=db, limit=args.limit, dry_run=not args.apply
        )
    finally:
        await _disconnect_if_needed(db, was)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--apply", action="store_true",
        help="Actually DELETE orphaned rows. Default: dry-run (report only).",
    )
    p.add_argument(
        "--limit", type=int, default=0,
        help="Max orphans to process this run (0 = all). Default 0.",
    )
    return p.parse_args()


def main() -> int:
    report = asyncio.run(_drive(_parse_args()))
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
