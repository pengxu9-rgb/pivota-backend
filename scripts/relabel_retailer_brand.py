#!/usr/bin/env python3
"""Relabel existing retailer rows of a brand family to its canonical spelling (services/brand_relabel.py).

  Dry run (default; writes nothing):
    python -m scripts.relabel_retailer_brand --family etude
  Apply (one transaction; the manifest is written and printed BEFORE the write -- it lands in the job log's
  jsonPayload as one JSON line; save it):
    python -m scripts.relabel_retailer_brand --family etude --apply --manifest /tmp/relabel_etude.json
  Revert from a manifest (guarded: only rows still carrying the relabelled values move back):
    python -m scripts.relabel_retailer_brand revert --manifest relabel_etude.json

`--family` is a curated_brand_feed.RETAILER_BRAND_SPELLINGS family: every spelling listed for it is
relabelled to RETAILER_BRAND_CANONICAL[family]. Needs DATABASE_URL: run it through scripts/ops/run_oneoff_job.sh.
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
from services.curated_brand_feed import RETAILER_BRAND_CANONICAL, RETAILER_BRAND_SPELLINGS  # noqa: E402


def family_spellings(family: str) -> List[str]:
    spellings = [k for k, v in RETAILER_BRAND_SPELLINGS.items() if v == family]
    if not spellings or family not in RETAILER_BRAND_CANONICAL:
        raise SystemExit(f"unknown brand family {family!r}")
    return spellings


async def run(args: argparse.Namespace) -> int:
    await database.connect()
    try:
        if args.command == "revert":
            m = json.loads(Path(args.manifest).read_text())
            counts = await brand_relabel.write_moves(database, m["moves"], reverse=True)
            refresh = await brand_relabel.refresh_after(database, m["moves"], source="brand_relabel_revert",
                                                        reverse=True)
            print("REVERTED " + brand_relabel.dumps({"run_id": m["run_id"], "counts": counts, "refresh": refresh}))
            return 0
        canonical = RETAILER_BRAND_CANONICAL[args.family]
        rows, neighbours = await brand_relabel.load(database, family_spellings(args.family), canonical)
        plan = brand_relabel.plan_relabel(rows, neighbours, canonical)
        by_host = {}
        for d in plan["moves"]:
            by_host[d["source_domain"]] = by_host.get(d["source_domain"], 0) + 1
        print("PLAN " + brand_relabel.dumps({"canonical": canonical, "candidates": len(rows), "counts": plan["counts"],
                                             "moves_by_host": by_host,
                                             "holds": [{k: h.get(k) for k in ("product_key", "title", "hold")}
                                                       for h in plan["holds"]][:40]}), flush=True)
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
    p.add_argument("command", nargs="?", default="plan", choices=["plan", "revert"])
    p.add_argument("--family")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--manifest")
    a = p.parse_args(argv)
    if a.command == "plan" and not a.family:
        p.error("--family is required")
    if a.command == "revert" and not a.manifest:
        p.error("revert requires --manifest")
    return asyncio.run(run(a))


if __name__ == "__main__":
    sys.exit(main())
