#!/usr/bin/env python3
"""Relabel existing retailer rows of a brand to one canonical spelling (services/brand_relabel.py).

  Dry run (default; writes nothing -- moves by host, reasons, and every HELD row):
    python -m scripts.relabel_retailer_brand --family etude
  Apply (one transaction; the manifest is STORED in identity_resolution_events BEFORE the write -- a job log
  cuts a large one -- and the run id printed):
    python -m scripts.relabel_retailer_brand --family etude --apply
  Revert a run (all or nothing: any row that moved since aborts it):
    python -m scripts.relabel_retailer_brand revert --run-id relabel_<hex>
  Re-run only the view / eligibility / trust rebuild of an applied (or reverted, with --reverse) run:
    python -m scripts.relabel_retailer_brand refresh --run-id relabel_<hex>
  (--manifest <file> instead of --run-id reads a manifest from a file.)

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
# A job log cuts a line at ~100 KB: status lines carry counts and a bounded sample, never an unbounded list.
HOLDS_SHOWN = 100
FAILED_KEYS_SHOWN = 50


def bounded(refresh: dict) -> dict:
    failed = refresh.get("failed_keys") or []
    return {**refresh, "failed_keys_total": len(failed), "failed_keys": failed[:FAILED_KEYS_SHOWN]}


FAMILIES = {
    # Measured in prod 2026-09-30 (read-only dry run of this plan): retailer rows off the canonical spelling.
    # Case-only (no content_key moves; the spelling family is already live for these):
    #   missha 413 (14 hosts), roundlab 31, bobbibrown 17, maybelline 12, makeupforever 4, byterry 4 -- 0 held.
    # Identity-changing (the spelling family must wait for this relabel -- services/brand_relabel.py ORDER):
    #   ohui 20 on smkoreabeauty.com (15 minted, 3 already grouped, 2 held would_split_its_product),
    #   jungsaemmool 5 on smkoreabeauty.com (5 minted, 0 held).
    # Each canonical is curated_brand_feed.RETAILER_BRAND_CANONICAL's for that family (pinned by a test).
    "missha": ("Missha", ["MISSHA", "Missha"]),
    "roundlab": ("Round Lab", ["ROUND LAB", "Round Lab"]),
    "bobbibrown": ("Bobbi Brown", ["BOBBI BROWN", "Bobbi Brown"]),
    "maybelline": ("Maybelline", ["MAYBELLINE", "Maybelline"]),
    "makeupforever": ("MAKE UP FOR EVER", ["Make Up For Ever", "MAKE UP FOR EVER"]),
    "byterry": ("BY TERRY", ["By Terry", "BY TERRY"]),
    # smkoreabeauty.com writes the Hangul name after the brand; _brand_key and PG [:alnum:] both keep Hangul.
    "ohui": ("O HUI", ["O HUI (오휘)", "O HUI"]),
    "jungsaemmool": ("JUNGSAEMMOOL", ["Jung Saem Mool", "JUNGSAEMMOOL"]),
    "etude": ("ETUDE", ["ETUDE HOUSE", "Etude House", "ETUDE", "Etude", "ÉTUDE HOUSE", "Étude House", "ÉTUDE",
                        "Étude"]),
}


async def run(args: argparse.Namespace) -> int:
    await database.connect()
    try:
        if args.command in ("revert", "refresh"):
            m = (json.loads(Path(args.manifest).read_text()) if args.manifest
                 else await brand_relabel.load_manifest(database, args.run_id))
            reverse = args.command == "revert" or args.reverse
            counts = (await brand_relabel.write_moves(database, m["moves"], reverse=True, run_id=m["run_id"])
                      if args.command == "revert" else None)
            refresh = await brand_relabel.refresh_after(database, m["moves"], reverse=reverse,
                                                        source=f"brand_relabel_{args.command}")
            print(args.command.upper() + " " + brand_relabel.dumps({"run_id": m["run_id"], "counts": counts,
                                                                     "refresh": bounded(refresh)}))
            return 0
        canonical, spellings = FAMILIES[args.family]
        rows, neighbours, seeds = await brand_relabel.load(database, spellings, canonical)
        plan = brand_relabel.plan_relabel(rows, neighbours, canonical, seeds)
        by_host = {}
        for d in plan["moves"]:
            by_host[d["source_domain"]] = by_host.get(d["source_domain"], 0) + 1
        print("PLAN " + brand_relabel.dumps({"canonical": canonical, "candidates": len(rows), "counts": plan["counts"],
                                             "moves_by_host": by_host,
                                             "holds_total": len(plan["holds"]),
                                             "holds": [{k: (str(h.get(k) or "")[:80]) for k in
                                                        ("product_key", "source_domain", "title", "hold")}
                                                       for h in plan["holds"][:HOLDS_SHOWN]]}), flush=True)
        if not args.apply:
            print("DRY-RUN -- re-run with --apply to relabel.")
            return 0
        if not plan["moves"]:
            print("nothing to relabel -- no write.")
            return 0
        manifest = brand_relabel.manifest_for(plan)
        await brand_relabel.store_manifest(database, manifest)  # durable BEFORE the write
        print(f"MANIFEST STORED run_id={manifest['run_id']} moves={len(manifest['moves'])}", flush=True)
        if args.manifest:
            Path(args.manifest).write_text(json.dumps(manifest, indent=1, default=str))
        counts = await brand_relabel.write_moves(database, manifest["moves"], run_id=manifest["run_id"])
        refresh = await brand_relabel.refresh_after(database, manifest["moves"], source="brand_relabel")
        print("APPLIED " + brand_relabel.dumps({"run_id": manifest["run_id"], "counts": counts,
                                                "refresh": bounded(refresh)}))
        return 0
    finally:
        await database.disconnect()


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", nargs="?", default="plan", choices=["plan", "revert", "refresh"])
    p.add_argument("--family", choices=sorted(FAMILIES))
    p.add_argument("--apply", action="store_true")
    p.add_argument("--manifest")
    p.add_argument("--run-id", dest="run_id")
    p.add_argument("--reverse", action="store_true", help="refresh: the manifest was reverted")
    a = p.parse_args(argv)
    if a.command == "plan" and not a.family:
        p.error("--family is required")
    if a.command in ("revert", "refresh") and not (a.manifest or a.run_id):
        p.error(f"{a.command} requires --run-id (or --manifest)")
    return asyncio.run(run(a))


if __name__ == "__main__":
    sys.exit(main())
