#!/usr/bin/env python3
"""Relabel existing retailer rows of a brand to one canonical spelling (services/brand_relabel.py).

  Dry run (default; writes nothing -- moves by host, reasons, and every HELD row):
    python -m scripts.relabel_retailer_brand --family etude
  Apply (one transaction; the manifest is written and printed BEFORE the write -- it lands in the job log's
  jsonPayload as one JSON line; save it):
    python -m scripts.relabel_retailer_brand --family etude --apply --manifest /tmp/relabel_etude.json
  Revert a manifest (all or nothing: any row that moved since aborts it):
    python -m scripts.relabel_retailer_brand revert --manifest relabel_etude.json
  Re-run only the view / eligibility / trust rebuild of an applied (or reverted, with --reverse) manifest:
    python -m scripts.relabel_retailer_brand refresh --manifest relabel_etude.json

ORDER (services/brand_relabel.py): relabel here, resolve every held row, and only THEN add the spelling family
to curated_brand_feed.RETAILER_BRAND_SPELLINGS -- a family live on the drain before that fails every re-crawl
of a store that still has an old-spelling row.

Needs DATABASE_URL: run it through scripts/ops/run_oneoff_job.sh.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.database import database  # noqa: E402
from services import brand_relabel  # noqa: E402

# (canonical, the spellings relabelled to it). Measured in prod 2026-09-28; accented spellings are listed because
# the family match keeps accented letters (Python casefold and PG lower() both do).
FAMILIES = {
    "etude": ("ETUDE", ["ETUDE HOUSE", "Etude House", "ETUDE", "Etude", "ÉTUDE HOUSE", "Étude House", "ÉTUDE",
                        "Étude"]),
}


async def run(args: argparse.Namespace) -> int:
    await database.connect()
    try:
        if args.command in ("revert", "refresh"):
            m = json.loads(Path(args.manifest).read_text())
            reverse = args.command == "revert" or args.reverse
            counts = (await brand_relabel.write_moves(database, m["moves"], reverse=True)
                      if args.command == "revert" else None)
            refresh = await brand_relabel.refresh_after(database, m["moves"], reverse=reverse,
                                                        source=f"brand_relabel_{args.command}")
            print(args.command.upper() + " " + brand_relabel.dumps({"run_id": m["run_id"], "counts": counts,
                                                                     "refresh": refresh}))
            return 0
        canonical, spellings = FAMILIES[args.family]
        rows, neighbours, seeds = await brand_relabel.load(database, spellings, canonical)
        plan = brand_relabel.plan_relabel(rows, neighbours, canonical, seeds)
        by_host = {}
        for d in plan["moves"]:
            by_host[d["source_domain"]] = by_host.get(d["source_domain"], 0) + 1
        print("PLAN " + brand_relabel.dumps({"canonical": canonical, "candidates": len(rows), "counts": plan["counts"],
                                             "moves_by_host": by_host,
                                             "holds": [{k: h.get(k) for k in ("product_key", "source_domain",
                                                                              "title", "hold")}
                                                       for h in plan["holds"]]}), flush=True)
        if not args.apply:
            print("DRY-RUN -- re-run with --apply --manifest <path> to relabel.")
            return 0
        if not args.manifest:
            print("--apply requires --manifest (the reversal record)", file=sys.stderr)
            return 2
        manifest = brand_relabel.manifest_for(plan)
        Path(args.manifest).write_text(json.dumps(manifest, indent=1, default=str))
        print(json.dumps(manifest, default=str), flush=True)  # BEFORE the write; outlives the container in Logging
        counts = await brand_relabel.write_moves(database, manifest["moves"])
        refresh = await brand_relabel.refresh_after(database, manifest["moves"], source="brand_relabel")
        print("APPLIED " + brand_relabel.dumps({"run_id": manifest["run_id"], "counts": counts, "refresh": refresh}))
        return 0
    finally:
        await database.disconnect()


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", nargs="?", default="plan", choices=["plan", "revert", "refresh"])
    p.add_argument("--family", choices=sorted(FAMILIES))
    p.add_argument("--apply", action="store_true")
    p.add_argument("--manifest")
    p.add_argument("--reverse", action="store_true", help="refresh: the manifest was reverted")
    a = p.parse_args(argv)
    if a.command == "plan" and not a.family:
        p.error("--family is required")
    if a.command in ("revert", "refresh") and not a.manifest:
        p.error(f"{a.command} requires --manifest")
    return asyncio.run(run(a))


if __name__ == "__main__":
    sys.exit(main())
