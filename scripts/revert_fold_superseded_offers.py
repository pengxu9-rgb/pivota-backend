#!/usr/bin/env python3
"""Undo one retailer-ingest run's shade-fold retire (services/retailer_ingest/fold_retire_revert.py).

DRY-RUN BY DEFAULT: lists the offers still retired `fold_superseded` by that run. Nothing is written without
--apply, and the write touches only rows still under that reason AND that run id.

  DATABASE_URL=... python3 scripts/revert_fold_superseded_offers.py --run-id rir_...
  DATABASE_URL=... python3 scripts/revert_fold_superseded_offers.py --run-id rir_... --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import List, Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


async def run(run_id: str, apply: bool) -> int:
    from db.database import database
    from services.retailer_ingest.fold_retire_revert import revert_fold_retire

    await database.connect()
    try:
        out = await revert_fold_retire(run_id, apply=apply, db=database)
    finally:
        await database.disconnect()
    print(json.dumps({"run_id": out["run_id"], "apply": out["apply"], "still_retired": len(out["offers"]),
                      "reverted": out["reverted"]}, indent=2))
    for row in out["offers"][:200]:
        print(f"  {row['offer_id']}  {row['source_system']}  {row['source_ref']}")
    if not apply:
        print("dry run: nothing written (pass --apply to revert these)")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run-id", required=True, help="the retailer_ingest run whose fold retire to undo")
    parser.add_argument("--apply", action="store_true", help="write the revert (default: dry run)")
    args = parser.parse_args(argv)
    return asyncio.run(run(args.run_id, args.apply))


if __name__ == "__main__":
    raise SystemExit(main())
