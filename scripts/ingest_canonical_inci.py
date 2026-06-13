"""Ingest canonical INCI for a product (supplier-input / backfill path).

The intake half of the canonical-sourcing engine (ADR-001): hand a product an
authoritative INCI list and it writes verified actives into the canonical record
by source precedence (brand-official > supplier > reseller). The supplier path
is `--source supplier_input`; a brand-official crawl/DB feed uses
`--source brand_official`.

Usage:
  python -m scripts.ingest_canonical_inci --product-key pk --inci "Aqua, Niacinamide, ..." --source brand_official
  python -m scripts.ingest_canonical_inci --product-key pk --inci-file inci.txt --source supplier_input --dry-run
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


async def _drive(args: argparse.Namespace, *, db: Any = database) -> dict:
    was = bool(getattr(db, "is_connected", False))
    if not was:
        await db.connect()
    try:
        return await ingest_canonical_inci(
            args.product_key, args.inci, args.source, db=db, dry_run=args.dry_run
        )
    finally:
        if not was:
            await db.disconnect()


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--product-key", required=True)
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--inci")
    group.add_argument("--inci-file")
    p.add_argument(
        "--source",
        default=INCI_SOURCE_SUPPLIER,
        choices=[INCI_SOURCE_BRAND_OFFICIAL, INCI_SOURCE_SUPPLIER, INCI_SOURCE_RESELLER],
    )
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    if args.inci_file:
        with open(args.inci_file, "r", encoding="utf-8") as handle:
            args.inci = handle.read().strip()
    return args


def main() -> int:
    print(json.dumps(asyncio.run(_drive(_parse_args())), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
