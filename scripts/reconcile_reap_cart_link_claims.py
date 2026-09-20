"""Inspect or repair completed Reap cart-link claims that have no attribution edge.

Run from the repository root with the intended DATABASE_URL explicitly set. A plain invocation
is read-only. Repairing requires --apply; neither mode calls Reap or changes a purchase/payment.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os

from db.database import database
from services.reap_agentic_purchase import reconcile_completed_cart_link_claims


async def _run(*, limit: int, apply: bool) -> dict:
    await database.connect()
    try:
        return await reconcile_completed_cart_link_claims(limit=limit, apply=apply)
    finally:
        await database.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=100, help="oldest claims to inspect (1–1000)")
    parser.add_argument("--apply", action="store_true", help="write only independently verified missing edges")
    args = parser.parse_args()
    if not (1 <= args.limit <= 1000):
        parser.error("--limit must be between 1 and 1000")
    if not (os.getenv("DATABASE_URL") or "").strip():
        parser.error("set DATABASE_URL explicitly to the database to inspect")
    print(json.dumps(asyncio.run(_run(limit=args.limit, apply=args.apply)), sort_keys=True))


if __name__ == "__main__":
    main()
