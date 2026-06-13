"""Resolve a product's canonical INCI from authoritative URL(s) and ingest it.

Ties the resolver (fetch brand-official PDP / INCI source -> extract INCI) to the
intake (write verified actives by source precedence). Candidate URLs are passed
in (the product's brand-official PDP, an INCI-DB page); the highest-priority one
that yields a valid INCI wins, tagged with its source URL.

Usage:
  python -m scripts.resolve_canonical_inci --product-key pk --url https://brand/pdp --source brand_official
  python -m scripts.resolve_canonical_inci --product-key pk --url URL1 --url URL2 --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

from db.database import database
from services.canonical_inci_intake import (
    INCI_SOURCE_BRAND_OFFICIAL,
    INCI_SOURCE_RESELLER,
    INCI_SOURCE_SUPPLIER,
    ingest_canonical_inci,
)
from services.canonical_inci_resolver import (
    http_fetch,
    resolve_inci_from_openbeautyfacts,
    resolve_inci_from_urls,
)


async def _drive(args: argparse.Namespace, *, db: Any = database) -> dict:
    # Structured INCI authority (Open Beauty Facts by barcode) first, then the
    # brand-official PDP URLs.
    resolved = None
    if args.barcode:
        resolved = await resolve_inci_from_openbeautyfacts(args.barcode, fetch=http_fetch)
    if resolved is None and args.url:
        resolved = await resolve_inci_from_urls(args.url, fetch=http_fetch)
    if resolved is None:
        return {
            "product_key": args.product_key,
            "status": "no_inci_resolved",
            "barcode": args.barcode,
            "urls_tried": args.url,
        }

    was = bool(getattr(db, "is_connected", False))
    if not was:
        await db.connect()
    try:
        result = await ingest_canonical_inci(
            args.product_key, resolved.raw_inci, args.source, db=db, dry_run=args.dry_run
        )
    finally:
        if not was:
            await db.disconnect()
    result["resolved_from"] = resolved.source_url
    return result


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--product-key", required=True)
    p.add_argument("--barcode", help="Resolve via Open Beauty Facts by barcode (tried first).")
    p.add_argument("--url", action="append", default=[], help="Candidate brand-official PDP URL (repeatable, priority order).")
    p.add_argument(
        "--source",
        default=INCI_SOURCE_BRAND_OFFICIAL,
        choices=[INCI_SOURCE_BRAND_OFFICIAL, INCI_SOURCE_SUPPLIER, INCI_SOURCE_RESELLER],
    )
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    if not args.barcode and not args.url:
        p.error("provide --barcode and/or at least one --url")
    return args


def main() -> int:
    print(json.dumps(asyncio.run(_drive(_parse_args())), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
