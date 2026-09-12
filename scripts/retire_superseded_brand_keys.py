#!/usr/bin/env python3
"""Retire catalog rows whose product_key was superseded by the brand-attribution fix.

REVERSIBLE tombstone, never a delete — the precedent set by
scripts/suppress_external_seed_rig_rows.py and scripts/merge_duplicate_canonicals.py:
`catalog_products.suppression_reason` + `suppressed_at` + `suppression_metadata`
carrying the run id, plus `external_product_seeds.status='inactive'` and the offer
cascade. `revert` undoes a whole run from its manifest.

WHY THIS EXISTS. `derive_product_key` hashes `(brand, title)`, so #2173 — which stopped
`brand_override` relabelling a sibling brand — MOVES the key of every product whose brand
it corrected. On misshaus.com that is 27 of 125 rows (A'pieu 17, CHOGONGJIN 7, Time
Revolution 2, Glow Skin 1). Re-onboarding writes the corrected rows under NEW keys and
`apply_ingest_plan` upserts on `product_key`, so it has no way to retire the old ones:
without this step the storefront ends up with 27 duplicate products, one of each pair
wrong-branded. `make_content_key` also hashes the brand, so the two rows of a pair do not
share a content_key and the duplicate-merge pipeline will never pair them either.

THE COHORT IS RE-DERIVED, NEVER PASSED IN. It comes from `records_for_brand` — the SAME
call the re-onboard makes — so the set retired here cannot drift from the set superseded
there. A key whose brand the fix did NOT move is not in the cohort by construction.

ORDER. Run this BEFORE the re-onboard. The two key sets are disjoint, so a suppressed
stale SKU cannot collide with a new one (`_SKU_SUPPRESSED_IDENTITY_SQL` guards on a
matching `product_key`, and ours differ) — but retiring first means the storefront is
never simultaneously serving both rows of a pair.

DRY-RUN BY DEFAULT. Nothing is written without --apply.

  # 1. plan
  DATABASE_URL=... python3 scripts/retire_superseded_brand_keys.py \
      --domain misshaus.com --brand Missha --category beauty/skincare

  # 2. apply (one transaction; writes a reversal manifest)
  DATABASE_URL=... python3 scripts/retire_superseded_brand_keys.py \
      --domain misshaus.com --brand Missha --category beauty/skincare \
      --apply --manifest /tmp/retire_misshaus.json

  # 3. undo the whole run
  DATABASE_URL=... python3 scripts/retire_superseded_brand_keys.py \
      revert --manifest /tmp/retire_misshaus.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db.database import database  # noqa: E402
from services.catalog_enrichment_agent.ingestion import derive_product_key  # noqa: E402
from services.catalog_offer_suppression import (  # noqa: E402
    cascade_for_suppressed_product_keys,
)
from services.curated_brand_feed import records_for_brand  # noqa: E402

REASON = "brand_attribution_key_supersede"

LIVE_ROWS_SQL = """
SELECT product_key, merchant_id, brand, title, suppression_reason, suppressed_at,
       suppression_metadata
FROM catalog_products
WHERE product_key = ANY(:keys)
"""

SUPPRESS_SQL = """
UPDATE catalog_products
SET suppression_reason = :reason,
    suppressed_at = COALESCE(suppressed_at, NOW()),
    suppression_metadata = CAST(:metadata AS jsonb),
    updated_at = NOW()
WHERE product_key = ANY(:keys)
  AND suppression_reason IS NULL
"""

SEEDS_FOR_KEYS_SQL = """
SELECT id, status FROM external_product_seeds
WHERE attached_product_key = ANY(:keys)
"""

DEACTIVATE_SEEDS_SQL = """
UPDATE external_product_seeds
SET status = 'inactive', updated_at = NOW()
WHERE attached_product_key = ANY(:keys)
  AND lower(coalesce(status, '')) = 'active'
RETURNING id
"""

UNSUPPRESS_SQL = """
UPDATE catalog_products
SET suppression_reason = :reason, suppressed_at = CAST(:suppressed_at AS timestamptz),
    suppression_metadata = CAST(:metadata AS jsonb), updated_at = NOW()
WHERE product_key = :key
"""

REACTIVATE_SEED_SQL = """
UPDATE external_product_seeds SET status = :status, updated_at = NOW() WHERE id = :id
"""


def _as_json_text(value: Any) -> Optional[str]:
    """A jsonb bind value as TEXT, or None. See the note in `revert`."""
    if value is None or isinstance(value, str):
        return value
    return json.dumps(value)


async def build_cohort(domain: str, brand: str, category_path: str) -> List[Dict[str, Any]]:
    """(stale_key, new_key, brand, title) for every record the fix re-keyed.

    `emit_real_variants=True` mirrors the re-onboard invocation; it does not affect the
    key, but keeping the two calls identical is what makes the cohorts provably the same.
    """
    recs = await records_for_brand(
        domain=domain, category_path=category_path, brand=brand, emit_real_variants=True
    )
    out: List[Dict[str, Any]] = []
    for rec in recs:
        pdp = rec.get("pdp") or {}
        title, new_brand = pdp.get("product_name"), pdp.get("brand")
        stale, new = derive_product_key(brand, title), derive_product_key(new_brand, title)
        if stale and new and stale != new:
            out.append({"stale_key": stale, "new_key": new, "brand": new_brand, "title": title})
    return out


async def plan(domain: str, brand: str, category_path: str) -> Dict[str, Any]:
    cohort = await build_cohort(domain, brand, category_path)
    stale_keys = [c["stale_key"] for c in cohort]
    new_keys = [c["new_key"] for c in cohort]
    rows = {r["product_key"]: dict(r) for r in await database.fetch_all(LIVE_ROWS_SQL, {"keys": stale_keys})}
    already_new = {r["product_key"] for r in await database.fetch_all(LIVE_ROWS_SQL, {"keys": new_keys})}
    seeds = [dict(r) for r in await database.fetch_all(SEEDS_FOR_KEYS_SQL, {"keys": stale_keys})]
    offers = await cascade_for_suppressed_product_keys(stale_keys, apply=False)

    present = [c for c in cohort if c["stale_key"] in rows]
    live = [c for c in present if not rows[c["stale_key"]].get("suppression_reason")]
    return {
        "domain": domain, "brand_override": brand, "category_path": category_path,
        "cohort": cohort, "rows": rows, "present": present, "live": live,
        "already_new": sorted(already_new), "seeds": seeds,
        "active_seeds": [s for s in seeds if str(s.get("status") or "").lower() == "active"],
        "offers": offers,
    }


def print_plan(p: Dict[str, Any]) -> None:
    print(f"domain            : {p['domain']}  (brand override {p['brand_override']!r})")
    print(f"re-keyed by #2173 : {len(p['cohort'])}")
    print(f"  present in catalog_products : {len(p['present'])}")
    print(f"  LIVE (would be tombstoned)  : {len(p['live'])}")
    print(f"  already suppressed          : {len(p['present']) - len(p['live'])}")
    print(f"  absent from catalog         : {len(p['cohort']) - len(p['present'])}")
    print(f"seeds attached    : {len(p['seeds'])}  (active, would deactivate: {len(p['active_seeds'])})")
    print(f"offers to cascade : {len(p['offers'])}")
    if p["already_new"]:
        print(f"NOTE: {len(p['already_new'])} replacement key(s) ALREADY present — a re-onboard has run.")
    print()
    for c in p["live"][:40]:
        print(f"  {c['brand']:<16} {c['title'][:46]}")
        print(f"      retire : {c['stale_key']}")
        print(f"      keeps  : {c['new_key']}")


async def apply(p: Dict[str, Any], manifest_path: str) -> Dict[str, Any]:
    keys = [c["stale_key"] for c in p["live"]]
    if not keys:
        print("nothing live to retire — no write.")
        return {}
    run_id = f"retire_{uuid.uuid4().hex[:12]}"
    metadata = json.dumps({
        "run_id": run_id, "reason": REASON, "pr": "pivota-backend#2173",
        "domain": p["domain"], "brand_override": p["brand_override"],
        "note": "product_key superseded when the brand override stopped relabelling a sibling brand",
        "at": datetime.now(timezone.utc).isoformat(),
    })
    # BEFORE-state first: a manifest written after the write cannot describe what it replaced.
    manifest = {
        "run_id": run_id, "reason": REASON, "domain": p["domain"],
        "brand_override": p["brand_override"], "category_path": p["category_path"],
        "at": datetime.now(timezone.utc).isoformat(),
        "products": [
            {
                "product_key": k,
                "prior_suppression_reason": p["rows"][k].get("suppression_reason"),
                "prior_suppressed_at": (
                    p["rows"][k]["suppressed_at"].isoformat()
                    if p["rows"][k].get("suppressed_at") else None
                ),
                "prior_suppression_metadata": p["rows"][k].get("suppression_metadata"),
            } for k in keys
        ],
        "seeds": [{"id": str(s["id"]), "prior_status": s.get("status")} for s in p["active_seeds"]],
    }
    Path(manifest_path).write_text(json.dumps(manifest, indent=1, default=str))
    print(f"manifest written BEFORE the write: {manifest_path}")
    # AND to stdout. This script's normal home is a Cloud Run Job, whose filesystem dies
    # with the container — a manifest that exists only at `manifest_path` is gone the
    # moment the job ends, which is to say the run is not actually revertible. Printing it
    # puts the reversal record in Cloud Logging, where it outlives the container. Bounded:
    # one small object per retired key.
    print("----8<---- MANIFEST BEGIN ----8<----")
    print(json.dumps(manifest, default=str))
    print("----8<---- MANIFEST END ----8<----")

    async with database.transaction():
        await database.execute(SUPPRESS_SQL, {"reason": REASON, "metadata": metadata, "keys": keys})
        seed_rows = await database.fetch_all(DEACTIVATE_SEEDS_SQL, {"keys": keys})
        offer_ids = await cascade_for_suppressed_product_keys(keys, apply=True)
        after = await database.fetch_all(LIVE_ROWS_SQL, {"keys": keys})
        unsuppressed = [r["product_key"] for r in after if not r["suppression_reason"]]
        if unsuppressed:
            raise RuntimeError(f"tombstone did not land on {len(unsuppressed)} row(s): {unsuppressed[:5]}")
    counts = {"products": len(keys), "seeds": len(seed_rows), "offers": len(offer_ids)}
    print(f"applied: {counts}  run_id={run_id}")
    return counts


async def revert(manifest_path: str) -> None:
    m = json.loads(Path(manifest_path).read_text())
    async with database.transaction():
        for row in m["products"]:
            await database.execute(UNSUPPRESS_SQL, {
                "key": row["product_key"], "reason": row["prior_suppression_reason"],
                "suppressed_at": row["prior_suppressed_at"],
                # `CAST(:metadata AS jsonb)` wants TEXT. The driver hands a jsonb column
                # back as a dict, the manifest round-trips it as one, and binding a dict
                # to a text cast fails -- in `revert`, which is the one path that must
                # not fail. Serialise anything that is not already a string.
                "metadata": _as_json_text(row["prior_suppression_metadata"]),
            })
        for s in m.get("seeds") or []:
            await database.execute(REACTIVATE_SEED_SQL, {"id": s["id"], "status": s["prior_status"]})
    print(f"reverted run {m['run_id']}: {len(m['products'])} product(s), {len(m.get('seeds') or [])} seed(s). "
          "Offer suppression is reverted by services.catalog_offer_suppression.revert_offer_suppression.")


async def run(args: argparse.Namespace) -> int:
    await database.connect()
    try:
        if args.command == "revert":
            await revert(args.manifest)
            return 0
        p = await plan(args.domain, args.brand, args.category)
        print_plan(p)
        if not args.apply:
            print("\nDRY-RUN — re-run with --apply (and --manifest) to tombstone.")
            return 0
        if not args.manifest:
            print("--apply requires --manifest (the reversal record)", file=sys.stderr)
            return 2
        await apply(p, args.manifest)
        return 0
    finally:
        await database.disconnect()


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", nargs="?", default="plan", choices=["plan", "revert"])
    p.add_argument("--domain")
    p.add_argument("--brand")
    p.add_argument("--category", default="beauty/skincare")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--manifest")
    a = p.parse_args(argv)
    if a.command != "revert" and not (a.domain and a.brand):
        p.error("--domain and --brand are required")
    if a.command == "revert" and not a.manifest:
        p.error("revert requires --manifest")
    return asyncio.run(run(a))


if __name__ == "__main__":
    raise SystemExit(main())
