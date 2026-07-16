"""DeepSeek judge lane CLI — resolve a brand's StyleKorean MINT residue against
its ENUMERATED brand-official catalog, and (optionally) attach SK offers for
auto-confidence matches whose official product already exists in our catalog.

    # dry: judge only, write proposals
    DATABASE_URL=... DEEPSEEK_API_KEY=... python3 scripts/judge_stylekorean_residue.py \
        --plan wave2_plan_skin1004.jsonl --brand-official-domain skin1004.com \
        --out skin1004_judged.jsonl

    # apply: also attach SK offers for AUTO verdicts (>= threshold) whose
    # official product resolves to an existing catalog row
    ... --apply-offers

Verdict buckets: auto (>= threshold, attachable), review (below threshold /
unparseable — HITL), no_match, no_candidates. Never mints, never merges.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.database import database  # noqa: E402
from scripts.attach_retailer_offer import (  # noqa: E402
    attach_retailer_offer,
    build_retailer_offer_row,
)
from scripts.ingest_stylekorean_brand import _load_our_rows, _offer_eligible  # noqa: E402
from services.pdp_matcher.retailer_match import build_match_index, match_record  # noqa: E402
from services.retailer_ingest import stylekorean as sk  # noqa: E402
from services.retailer_ingest.brand_official import fetch_official_records  # noqa: E402
from services.retailer_ingest.official_match_judge import (  # noqa: E402
    AUTO_ATTACH_THRESHOLD,
    judge_residue_items,
)


async def _drive(args: argparse.Namespace) -> int:
    plan = [json.loads(l) for l in open(args.plan) if l.strip()]
    mint = [p for p in plan if p.get("decision") == "mint"]
    if args.limit:
        mint = mint[: args.limit]
    print(f"[1/4] plan {args.plan}: {len(plan)} rows, judging {len(mint)} MINT residue items")

    print(f"[2/4] enumerating brand-official catalog {args.brand_official_domain} ...")
    official = await fetch_official_records(
        domain=args.brand_official_domain, brand=None, category_path=args.category_path,
    )
    print(f"      {len(official)} official products")

    print(f"[3/4] judging (DeepSeek, threshold {args.threshold}) ...")
    result = await judge_residue_items(mint, official, threshold=args.threshold)
    counts = {k: len(v) for k, v in result.items()}
    print(f"      auto={counts['auto']} review={counts['review']} "
          f"no_match={counts['no_match']} no_candidates={counts['no_candidates']}")
    if args.out:
        with open(args.out, "w") as f:
            for bucket, rows in result.items():
                for r in rows:
                    f.write(json.dumps({"bucket": bucket, **r}, ensure_ascii=False, default=str) + "\n")
        print(f"      wrote proposals -> {args.out}")
    for r in result["auto"][:10]:
        print(f"      AUTO {r['verdict']['confidence']:.2f}  SK '{r['item']['title'][:40]}' == "
              f"OFFICIAL '{(r['official'].get('pdp') or {}).get('product_name','')[:40]}'")

    print("[4/4] " + ("attaching offers for AUTO verdicts ..." if args.apply_offers
                      else "dry-run — no offer writes (use --apply-offers)"))
    if not args.apply_offers:
        return 0

    await database.connect()
    try:
        brands = sorted({str(r["item"].get("brand")) for r in result["auto"] if r["item"].get("brand")})
        our_rows = await _load_our_rows(brands)
        index = build_match_index(our_rows)
        attached = no_target = skipped = 0
        for r in result["auto"]:
            pdp = r["official"].get("pdp") or {}
            target = match_record(index, pdp.get("brand"), pdp.get("product_name"))
            if not target:
                # official product not (yet) a catalog row — e.g. a drift-suspect
                # that was never minted. Needs mint/merge first; report, don't guess.
                no_target += 1
                continue
            item = r["item"]
            if not _offer_eligible(item):
                skipped += 1
                continue
            row = build_retailer_offer_row(
                product_key=target["product_key"], merchant_id=sk.MERCHANT_ID,
                merchant_name=sk.MERCHANT_NAME, retailer_url=item["url"], market=sk.MARKET,
                currency=item.get("currency") or "USD", price=float(item["price"]),
                availability=item.get("record", {}).get("offers", [{}])[0].get("availability", "in_stock"),
            )
            await attach_retailer_offer(row)
            attached += 1
        print(f"      attached {attached} offers | no catalog target {no_target} | ineligible {skipped}")
    finally:
        await database.disconnect()
    return 0


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--plan", required=True, help="brand plan JSONL from ingest_stylekorean_brand --out")
    p.add_argument("--brand-official-domain", required=True)
    p.add_argument("--category-path", default="beauty/skincare")
    p.add_argument("--threshold", type=float, default=AUTO_ATTACH_THRESHOLD)
    p.add_argument("--limit", type=int, default=0, help="judge only the first N residue items")
    p.add_argument("--out", default=None, help="write judged proposals JSONL (all buckets)")
    p.add_argument("--apply-offers", action="store_true",
                   help="attach SK offers for AUTO verdicts with an existing catalog target")
    return p.parse_args()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_drive(_parse_args())))
