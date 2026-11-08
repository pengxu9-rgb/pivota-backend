"""Backfill the rail-neutral purchase ledger (migration 264) from the Reap purchase table.

Writes one `agent_purchases` parent for every Reap purchase that has none, oldest first, in
batches, until none are missing. Idempotent: a second run writes nothing, and it is safe beside
live traffic (every write is ON CONFLICT DO NOTHING on (rail, rail_purchase_id)). It reads and
copies only ids, owner columns and created_at: no PII, and it never touches a Reap row.

    python scripts/backfill_agent_purchases.py            # write
    python scripts/backfill_agent_purchases.py --dry-run  # count only

Exits 1 when any purchase could not be given a parent (each is logged; the last line counts what is
still missing), 0 otherwise.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections import Counter
from typing import List, Optional


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--batch", type=int, default=500, help="rows per batch (1..5000)")
    parser.add_argument("--max-batches", type=int, default=1000, help="stop after this many batches")
    parser.add_argument("--dry-run", action="store_true", help="count missing parents, write nothing")
    return parser


async def run(args: argparse.Namespace) -> int:
    from db.database import database
    import db.agent_purchase_ledger as ledger

    await database.connect()
    try:
        if args.dry_run:
            try:
                print(f"missing_parents={await ledger.count_missing_reap_parents()}")
            except Exception as exc:  # noqa: BLE001 - before the first boot's self-heal the table is absent
                print(f"missing_parents=unknown ({type(exc).__name__}: is agent_purchases built yet?)")
            return 0
        await ledger.ensure_agent_purchase_schema()
        total = failed = 0
        for batch in range(args.max_batches):
            tally: Counter = Counter()
            parented = await ledger.backfill_reap_parents(limit=args.batch, tally=tally)
            total += parented
            failed += tally["failed"]
            print(f"batch={batch} parented={parented} total={total} failed={tally['failed']} "
                  f"skipped_after_failure={tally['deferred']} stopped={tally['stopped']}")
            # Done only when a batch neither parented nor failed: a batch whose rows all failed (and
            # stopped) is followed by one that skips them, so failing rows cannot hide the rest.
            if parented == 0 and not tally["failed"]:
                break
        print(f"done parented={total} failed={failed} missing_parents={await ledger.count_missing_reap_parents()}")
        return 1 if failed else 0
    finally:
        await database.disconnect()


def main(argv: Optional[List[str]] = None) -> int:
    return asyncio.run(run(build_parser().parse_args(argv)))


if __name__ == "__main__":
    sys.path.insert(0, os.getcwd())
    raise SystemExit(main())
