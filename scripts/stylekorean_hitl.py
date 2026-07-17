#!/usr/bin/env python3
"""HITL lane for StyleKorean judge leftovers: no-target mints + duplicate merges.

Two leftover classes the deterministic + judge lanes correctly REFUSED to
write, queued here for a human decision:

  mint_and_attach — a judge AUTO verdict certified SK item == brand-official
      product, but that official product has no catalog row (it was a drift
      suspect routed to residue, or outside the net-new mint set). Approving
      mints the official record through the sanctioned brand-official ingest
      (canonical-first, ADR-001) and attaches the SK offer(s).

  merge_duplicate — two canonicals for one physical product (e.g. the missha
      cross-lane pairs: SK-titled vs official-titled, different content_keys,
      both carrying offers). PROPOSE-ONLY in v1: merging retires a canonical,
      which is a founder-level decision; apply reports and skips these.

Workflow:
    # 1. emit proposals (reads judge proposal files + live catalog; no writes)
    python3 scripts/stylekorean_hitl.py emit \
        --proposals 'judge_proposals_*.jsonl' --out hitl_review.jsonl

    # 2. human review: set "status": "approved" on rows to act on

    # 3. apply approved rows (writes; single-writer lock)
    python3 scripts/stylekorean_hitl.py apply --reviewed hitl_review.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import json
import os
import sys
from collections import defaultdict
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.database import database  # noqa: E402
from scripts.ingest_stylekorean_brand import (  # noqa: E402
    _attach_offer_items,
    _load_our_rows,
)
from services.pdp_matcher.line_alias import alias_match_keys  # noqa: E402
from services.pdp_matcher.retailer_match import (  # noqa: E402
    build_match_index,
    match_record,
    retailer_match_key,
)
from services.retailer_ingest import brand_official as bo  # noqa: E402
from services.retailer_ingest.single_writer_lock import (  # noqa: E402
    SingleWriterLockError,
    retailer_ingest_lock,
)


def _official_identity(official: Dict[str, Any]) -> str:
    pdp = official.get("pdp") or {}
    return f"{pdp.get('brand', '')}::{pdp.get('product_name', '')}"


async def _emit(args: argparse.Namespace) -> int:
    files = sorted(glob.glob(args.proposals))
    if not files:
        print(f"no proposal files match {args.proposals!r}")
        return 1
    autos: List[Dict[str, Any]] = []
    for f in files:
        for l in open(f):
            if l.strip():
                r = json.loads(l)
                if r.get("bucket") == "auto":
                    r["_source_file"] = os.path.basename(f)
                    autos.append(r)
    print(f"[emit] {len(autos)} AUTO verdicts across {len(files)} file(s)")

    await database.connect()
    brands = sorted({str(r["item"].get("brand")) for r in autos if r["item"].get("brand")})
    our_rows = await _load_our_rows(brands)
    index = build_match_index(our_rows)

    # mint_and_attach: still-unresolved no-target verdicts, one proposal per
    # OFFICIAL product (several SK size variants may certify to one official).
    by_official: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in autos:
        pdp = (r.get("official") or {}).get("pdp") or {}
        target = match_record(index, pdp.get("brand"), pdp.get("product_name")) or \
            match_record(index, r["item"].get("brand"), pdp.get("product_name"))
        if not target:
            by_official[_official_identity(r["official"])].append(r)

    proposals: List[Dict[str, Any]] = []
    for ident, group in sorted(by_official.items()):
        proposals.append({
            "kind": "mint_and_attach",
            "status": "pending",
            "official": group[0]["official"],
            "items": [g["item"] for g in group],
            "verdict_confidences": [
                (g.get("verdict") or {}).get("confidence") for g in group
            ],
            "source_files": sorted({g["_source_file"] for g in group}),
        })
    print(f"[emit] mint_and_attach proposals: {len(proposals)} official product(s) "
          f"({sum(len(p['items']) for p in proposals)} SK verdicts)")

    # merge_duplicate: same-brand canonicals collapsing to one match key (or
    # line-alias key) under DIFFERENT content_keys = duplicate canonicals.
    key_groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in our_rows:
        keys = [retailer_match_key(row.get("brand"), row.get("title"))]
        # include_suncare=False: SPF30/SPF50 TRUE variants share a suncare-
        # stripped alias key — proposing them as merge_duplicate would invite
        # a wrong human merge. Line-phrase aliases only.
        keys.extend(alias_match_keys(row.get("brand"), row.get("title"),
                                     include_suncare=False))
        for key in keys:
            if key:
                key_groups[key].append(dict(row))
    seen_pairs: set = set()
    merges: List[Dict[str, Any]] = []
    for key, rows in sorted(key_groups.items()):
        cks = {str(r.get("content_key")) for r in rows if r.get("content_key")}
        if len(cks) < 2:
            continue
        pair_id = "|".join(sorted(str(r.get("product_key")) for r in rows))
        if pair_id in seen_pairs:
            continue
        seen_pairs.add(pair_id)
        merges.append({
            "kind": "merge_duplicate",
            "status": "pending",
            "match_key": key,
            "rows": [{k: r.get(k) for k in
                      ("product_key", "content_key", "brand", "title", "pdp_scope")}
                     for r in rows],
        })
    print(f"[emit] merge_duplicate proposals: {len(merges)}")
    await database.disconnect()

    with open(args.out, "w") as f:
        for p in proposals + merges:
            f.write(json.dumps(p, ensure_ascii=False, default=str) + "\n")
    print(f"[emit] wrote {len(proposals) + len(merges)} proposals -> {args.out}")
    print('      review, set "status": "approved" on rows to act on, then run apply.')
    return 0


async def _apply(args: argparse.Namespace) -> int:
    rows = [json.loads(l) for l in open(args.reviewed) if l.strip()]
    approved = [r for r in rows if r.get("status") == "approved"]
    mints = [r for r in approved if r.get("kind") == "mint_and_attach"]
    merges = [r for r in approved if r.get("kind") == "merge_duplicate"]
    print(f"[apply] {len(approved)} approved of {len(rows)} "
          f"({len(mints)} mint_and_attach, {len(merges)} merge_duplicate)")
    if merges:
        print(f"[apply] SKIPPING {len(merges)} merge_duplicate proposal(s) — merges "
              "retire a canonical and are not automated in v1 (founder review).")
    if not mints:
        return 0

    await database.connect()
    try:
        async with retailer_ingest_lock(database):
            for p in mints:
                official = p["official"]
                pdp = official.get("pdp") or {}
                brand = pdp.get("brand") or (p["items"][0].get("brand") if p["items"] else None)
                domain = pdp.get("source_domain") or ""
                if not brand:
                    print("  WARN approved row has no brand — skipped (malformed proposal)")
                    continue
                print(f"  MINT {brand} | {pdp.get('product_name', '')[:56]}")
                # already-have guard: never duplicate a canonical someone minted
                # since emit time.
                our_rows = await _load_our_rows([str(brand)])
                net_new, already, suspects = bo.filter_net_new([official], our_rows)
                if not net_new:
                    reason = ("already in catalog" if already
                              else "suspect-drift vs an existing row (needs merge review)")
                    print(f"        {reason} — skipping mint")
                else:
                    summary = await bo.ingest_brand_official(
                        official_records=net_new, brand=str(brand), domain=str(domain),
                        apply=True, db=database, batch=True,
                    )
                    print(f"        ingested: {summary['applied']}")
                # attach the certified SK offers onto the (now-present) canonical
                our_rows2 = await _load_our_rows([str(brand)])
                index2 = build_match_index(our_rows2)
                attach_items = []
                for item in p["items"]:
                    target = match_record(index2, pdp.get("brand"), pdp.get("product_name")) or \
                        match_record(index2, item.get("brand"), pdp.get("product_name"))
                    if target:
                        attach_items.append({**item, "matched_product_key": target["product_key"]})
                if attach_items:
                    a, s = await _attach_offer_items(attach_items)
                    print(f"        attached {a} SK offer(s) ({s} skipped/collapsed)")
                else:
                    print("        WARN no catalog target after mint — offer not attached")
    except SingleWriterLockError as exc:
        print(f"[lock] {exc} — exiting without writes.")
        return 1
    finally:
        await database.disconnect()
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("emit", help="write pending HITL proposals (no DB writes)")
    e.add_argument("--proposals", required=True,
                   help="glob of judge proposal JSONL files")
    e.add_argument("--out", required=True)
    a = sub.add_parser("apply", help="apply APPROVED proposals (writes)")
    a.add_argument("--reviewed", required=True)
    args = p.parse_args()
    return asyncio.run(_emit(args) if args.cmd == "emit" else _apply(args))


if __name__ == "__main__":
    raise SystemExit(main())
