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
from scripts.ingest_stylekorean_brand import (  # noqa: E402
    _attach_offer_items,
    _load_our_rows,
)
from services.pdp_matcher.retailer_match import build_match_index, match_record  # noqa: E402
from services.retailer_ingest import stylekorean as sk  # noqa: E402
from services.retailer_ingest.brand_official import (  # noqa: E402
    dominant_brand,
    fetch_official_records,
)
from services.retailer_ingest.single_writer_lock import (  # noqa: E402
    SingleWriterLockError,
    retailer_ingest_lock,
)
from services.retailer_ingest.official_match_judge import (  # noqa: E402
    AUTO_ATTACH_THRESHOLD,
    judge_residue_items,
)


async def _attach_auto_verdicts(auto_rows: List[Dict[str, Any]]) -> None:
    """Attach SK offers for AUTO verdicts. Shared by the live judge path and the
    --apply-from path so both go through the SAME B1-safe machinery: resolve each
    verdict's official product to a catalog target, stamp matched_product_key,
    dedup via the deterministic picker, and never clobber an offer the ATTACH
    lane already placed. Each row is {"item": <plan row>, "official": <record>}."""
    brands = sorted({str(r["item"].get("brand")) for r in auto_rows if r["item"].get("brand")})
    our_rows = await _load_our_rows(brands)
    index = build_match_index(our_rows)

    candidates: List[Dict[str, Any]] = []
    no_target = 0
    target_pks = set()
    for r in auto_rows:
        pdp = (r.get("official") or {}).get("pdp") or {}
        target = match_record(index, pdp.get("brand"), pdp.get("product_name"))
        if not target:
            # Vendor-brand fallback: a Shopify vendor string can drift from our
            # catalog brand beyond what match_brand strips ("Anua US"/"Anua
            # Global" vs "Anua" — the anua 20/20 no-target anomaly, 2026-07-17).
            # The verdict already certifies item == official, and the mint lane
            # ingests officials under the ITEM-side dominant brand, so re-keying
            # the official title with the item's brand is deterministic parity,
            # not a guess.
            target = match_record(index, r["item"].get("brand"), pdp.get("product_name"))
        if not target:
            # official not (yet) a catalog row (e.g. an un-minted drift suspect).
            # Needs mint/merge first; report, never guess a target.
            no_target += 1
            continue
        candidates.append({**r["item"], "matched_product_key": target["product_key"]})
        target_pks.add(target["product_key"])

    # Don't clobber the ATTACH lane: if a target already carries a StyleKorean
    # offer (its deterministic pick from the direct-match lane), leave it — the
    # judge lane only fills canonicals the ATTACH lane didn't reach.
    already = set()
    if target_pks:
        rows = await database.fetch_all(
            "SELECT DISTINCT product_key FROM catalog_offers "
            "WHERE merchant_id = :m AND product_key = ANY(:pks)",
            {"m": sk.MERCHANT_ID, "pks": list(target_pks)},
        )
        already = {dict(r)["product_key"] for r in rows}
    fresh = [c for c in candidates if c["matched_product_key"] not in already]
    skipped_existing = len(candidates) - len(fresh)

    applied, skipped = await _attach_offer_items(fresh)
    print(f"      attached {applied} offers | no catalog target {no_target} | "
          f"already-had-SK-offer {skipped_existing} | ineligible/collapsed {skipped}")


async def _drive_apply_from(args: argparse.Namespace) -> int:
    """Attach offers from a saved DRY proposals file — applies EXACTLY the auto
    verdicts already produced/reviewed, with no re-judge (no DeepSeek re-spend,
    no LLM nondeterminism)."""
    auto = [
        json.loads(l) for l in open(args.apply_from)
        if l.strip() and json.loads(l).get("bucket") == "auto"
    ]
    print(f"[apply-from] {args.apply_from}: {len(auto)} AUTO verdicts to attach")
    if not auto:
        return 0
    await database.connect()
    try:
        async with retailer_ingest_lock(database):
            await _attach_auto_verdicts(auto)
    except SingleWriterLockError as exc:
        print(f"[lock] {exc} — exiting without writes.")
        return 1
    finally:
        await database.disconnect()
    return 0


async def _drive(args: argparse.Namespace) -> int:
    if args.apply_from:
        return await _drive_apply_from(args)
    plan = [json.loads(l) for l in open(args.plan) if l.strip()]
    mint = [p for p in plan if p.get("decision") == "mint"]
    # brand from the FULL plan (before --limit), mirroring the MINT lane's
    # dominant_brand(mint) or dominant_brand(attach) fallback chain.
    brand_name = dominant_brand(mint) or dominant_brand(plan)
    if args.limit:
        mint = mint[: args.limit]
    print(f"[1/4] plan {args.plan}: {len(plan)} rows, judging {len(mint)} MINT residue items")

    # Brand parity with the MINT lane (ingest_stylekorean_brand step [5/5]): fetch
    # officials under the plan's dominant brand, not the raw Shopify vendor —
    # minted canonicals carry the dominant brand, so vendor-branded records
    # ("Anua US") would never resolve a catalog target at attach time.
    print(f"[2/4] enumerating brand-official catalog {args.brand_official_domain} "
          f"(brand={brand_name!r}) ...")
    official = await fetch_official_records(
        domain=args.brand_official_domain, brand=brand_name, category_path=args.category_path,
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
        async with retailer_ingest_lock(database):
            await _attach_auto_verdicts(result["auto"])
    except SingleWriterLockError as exc:
        print(f"[lock] {exc} — exiting without writes.")
        return 1
    finally:
        await database.disconnect()
    return 0


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--plan", help="brand plan JSONL from ingest_stylekorean_brand --out (judge mode)")
    p.add_argument("--brand-official-domain", help="brand's official Shopify domain (judge mode)")
    p.add_argument("--category-path", default="beauty/skincare")
    p.add_argument("--threshold", type=float, default=AUTO_ATTACH_THRESHOLD)
    p.add_argument("--limit", type=int, default=0, help="judge only the first N residue items")
    p.add_argument("--out", default=None, help="write judged proposals JSONL (all buckets)")
    p.add_argument("--apply-offers", action="store_true",
                   help="attach SK offers for AUTO verdicts with an existing catalog target")
    p.add_argument("--apply-from", default=None,
                   help="attach offers from a saved proposals JSONL (applies exactly its AUTO "
                        "verdicts; no re-judge). Mutually exclusive with judge mode.")
    args = p.parse_args()
    if not args.apply_from and (not args.plan or not args.brand_official_domain):
        p.error("judge mode requires --plan and --brand-official-domain (or use --apply-from)")
    return args


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_drive(_parse_args())))
