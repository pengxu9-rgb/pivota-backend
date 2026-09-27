#!/usr/bin/env python3
"""Deactivate the 4 ownist.com seeds the step-5 lane-4 twin cut left ACTIVE.

REVERSIBLE, never a delete, and never a re-point. `status='inactive'` only; the
`attached_product_key` is KEPT (the tombstone every other retire path leaves --
scripts/retire_superseded_brand_keys.py DEACTIVATE_SEEDS_SQL,
services/identity_resolution.py DEACTIVATE_SEEDS_SQL -- revert needs it).

WHY. scripts/step5_lane4_ownist_twin_cut.py (2026-07-10, founder sign-off "suppress seed
mirror, keep first-party") suppressed the 4 catalog_products rows of these seeds with
`cross_merchant_redundant_external_seed` and left the seeds "deliberately untouched" after
migration 139's precedent -- but 139's 50 rows were ALSO in catalog_source_quarantine, and
ownist.com never was. Two days later (2026-07-12) the first-party twins it kept
(merch_test_ownist_001) were themselves retired as `demo_retired_2026_07`. Measured prod
2026-09-27:
  - all 4 seeds status='active', market US, USD, domain not quarantined, re-crawled nightly
    (last attempt 2026-09-24);
  - services/external_seed_search.fetch_external_seed_rows, called the way find_products_multi
    calls it (no partition, US buyer, only_unattached=False), returns all 4 for "ownist", and
    should_block_external_referral_runtime blocks none of them;
  - 2 of the 4 URLs are ALSO served by a live kbeauty_crawl seed (serving_eligible) -> a
    duplicate card; the 2 garden gift sets have no other presence.
The seed lane does not join catalog_products, so a product suppression does not reach it;
the seed status is the switch that does.

PRECONDITIONS, ALL-OR-NOTHING. Every seed must still be on ownist.com, still attached to the
product the lane-4 cut suppressed, and that product must still carry the cut's tombstone
(reason + suppression_metadata.script). If any one fails -- a re-point, a reverted
tombstone, a missing row -- nothing is written: a reverted tombstone means someone decided
the product should serve, and its seed should stay active.

DRY-RUN BY DEFAULT. Runs as a Cloud Run one-off (the image does not carry this file until it
is merged and deployed, so pass it inline):

  # 1. plan
  scripts/ops/run_oneoff_job.sh -c "$(cat scripts/deactivate_lane4_ownist_mirror_seeds.py)"
  # 2. apply (one transaction; the manifest is printed to stdout BEFORE the write)
  scripts/ops/run_oneoff_job.sh -c "$(cat scripts/deactivate_lane4_ownist_mirror_seeds.py)" --apply
  # 3. undo, from the manifest line of step 2's log
  scripts/ops/run_oneoff_job.sh -c "$(cat scripts/deactivate_lane4_ownist_mirror_seeds.py)" \\
      revert --manifest-json '<the MANIFEST line>'
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
from typing import Any, Dict, List, Optional, Tuple

# `python -c` has no __file__; the job's working directory is the repo root.
_here = globals().get("__file__")
ROOT = Path(_here).resolve().parent.parent if _here else Path(os.getcwd())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DOMAIN = "ownist.com"
CUT_REASON = "cross_merchant_redundant_external_seed"
CUT_SCRIPT = "step5_lane4_ownist_twin_cut"

# seed id -> the attached_product_key the lane-4 cut suppressed (read from prod 2026-09-27).
SEEDS: Dict[str, str] = {
    "eps_b81018b0147d2b7103a9ff50": "prod::external_seed::external_seed::ext_52e9fce8bee97af26d6f77db",
    "eps_8392f5063ab5890a5f8388ce": "prod::external_seed::external_seed::ext_8b6bcbe031a743c1e607aa6a",
    "eps_c563537f86299bf556bd6761": "prod::external_seed::external_seed::ext_3b2f5eaa044e309bf442a047",
    "eps_ef1189fcc1414c309f574ee0": "prod::external_seed::external_seed::ext_3dab0df3312e80e8dac97452",
}

SEEDS_SQL = """
SELECT id, status, domain, title, canonical_url, attached_product_key, updated_at
FROM external_product_seeds
WHERE id = ANY(:ids)
"""

PRODUCTS_SQL = """
SELECT product_key, suppressed_at, suppression_reason, suppression_metadata
FROM catalog_products
WHERE product_key = ANY(:keys)
"""

# The attached key is re-checked IN the write, not only in the plan: a re-point between the
# plan and the apply must not deactivate a seed that now fronts a different product.
DEACTIVATE_SQL = """
UPDATE external_product_seeds
SET status = 'inactive', updated_at = NOW()
WHERE id = :id
  AND attached_product_key = :key
  AND lower(coalesce(status, '')) = 'active'
RETURNING id
"""

REACTIVATE_SQL = """
UPDATE external_product_seeds
SET status = :status, updated_at = NOW()
WHERE id = :id
  AND attached_product_key = :key
  AND lower(coalesce(status, '')) = 'inactive'
RETURNING id
"""


def _metadata(value: Any) -> Dict[str, Any]:
    """asyncpg hands jsonb back as TEXT unless a codec is set; accept either."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def classify(
    seeds: Dict[str, Dict[str, Any]], products: Dict[str, Dict[str, Any]]
) -> Tuple[List[Dict[str, Any]], List[str], List[str]]:
    """(to_deactivate, already_inactive, refusals) for SEEDS against the rows read.

    Pure, so the precondition logic is testable without a database.
    """
    to_deactivate: List[Dict[str, Any]] = []
    already: List[str] = []
    refusals: List[str] = []
    for seed_id, expected_key in SEEDS.items():
        seed = seeds.get(seed_id)
        if seed is None:
            refusals.append(f"{seed_id}: seed row not found")
            continue
        if seed.get("attached_product_key") != expected_key:
            refusals.append(
                f"{seed_id}: attached_product_key is {seed.get('attached_product_key')!r}, "
                f"expected {expected_key!r} (re-pointed since the lane-4 cut)"
            )
            continue
        if str(seed.get("domain") or "").strip().lower() != DOMAIN:
            refusals.append(f"{seed_id}: domain is {seed.get('domain')!r}, expected {DOMAIN!r}")
            continue
        product = products.get(expected_key)
        if product is None:
            refusals.append(f"{seed_id}: attached product {expected_key} not found")
            continue
        script = _metadata(product.get("suppression_metadata")).get("script")
        if (
            product.get("suppressed_at") is None
            or product.get("suppression_reason") != CUT_REASON
            or script != CUT_SCRIPT
        ):
            refusals.append(
                f"{seed_id}: product {expected_key} no longer carries the lane-4 tombstone "
                f"(suppressed_at={product.get('suppressed_at')}, "
                f"reason={product.get('suppression_reason')!r}, script={script!r})"
            )
            continue
        status = str(seed.get("status") or "").strip().lower()
        if status == "inactive":
            already.append(seed_id)
        elif status == "active":
            to_deactivate.append(seed)
        else:
            refusals.append(f"{seed_id}: unexpected status {seed.get('status')!r}")
    return to_deactivate, already, refusals


async def _read(database: Any) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    seeds = {
        str(dict(r)["id"]): dict(r)
        for r in await database.fetch_all(SEEDS_SQL, {"ids": list(SEEDS)})
    }
    products = {
        str(dict(r)["product_key"]): dict(r)
        for r in await database.fetch_all(PRODUCTS_SQL, {"keys": sorted(set(SEEDS.values()))})
    }
    return seeds, products


def build_manifest(to_deactivate: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "run_id": f"deact_{uuid.uuid4().hex[:12]}",
        "script": "deactivate_lane4_ownist_mirror_seeds",
        "cut_script": CUT_SCRIPT,
        "at": datetime.now(timezone.utc).isoformat(),
        "seeds": [
            {
                "id": str(s["id"]),
                "attached_product_key": s["attached_product_key"],
                "prior_status": s["status"],
                "prior_updated_at": s["updated_at"].isoformat() if s.get("updated_at") else None,
            }
            for s in to_deactivate
        ],
    }


async def _offer_report(keys: List[str]) -> Optional[int]:
    """Offers the product suppression would still cascade to -- REPORTED, never applied here."""
    try:
        from services.catalog_offer_suppression import cascade_for_suppressed_product_keys

        return len(await cascade_for_suppressed_product_keys(keys, apply=False))
    except Exception as exc:  # report-only; never block the plan on it
        print(f"offer report unavailable: {type(exc).__name__}: {exc}")
        return None


async def plan_and_apply(database: Any, apply: bool) -> int:
    seeds, products = await _read(database)
    to_deactivate, already, refusals = classify(seeds, products)
    for s in to_deactivate:
        print(f"  deactivate {s['id']}  {s.get('title')!r}  {s.get('canonical_url')}")
        print(f"      keeps attached_product_key {s['attached_product_key']}")
    for seed_id in already:
        print(f"  already inactive {seed_id}")
    for r in refusals:
        print(f"  REFUSE {r}")
    pending = await _offer_report(sorted(set(SEEDS.values())))
    print(
        f"seeds: {len(SEEDS)}  deactivate: {len(to_deactivate)}  already inactive: {len(already)}  "
        f"refused: {len(refusals)}  unsuppressed offers on the 4 products (not touched): {pending}"
    )
    if refusals:
        print("ABORT: a precondition failed -- nothing written.")
        return 1
    if not to_deactivate:
        print("nothing to deactivate -- no write.")
        return 0
    if not apply:
        print("DRY-RUN -- no changes written. Re-run with --apply to deactivate.")
        return 0

    manifest = build_manifest(to_deactivate)
    # BEFORE the write, and to stdout: a Cloud Run job's filesystem dies with the container,
    # so the log line is the only reversal record that outlives the run.
    print("----8<---- MANIFEST BEGIN ----8<----")
    print("MANIFEST " + json.dumps(manifest, default=str))
    print("----8<---- MANIFEST END ----8<----")

    async with database.transaction():
        done: List[str] = []
        for s in manifest["seeds"]:
            row = await database.fetch_one(DEACTIVATE_SQL, {"id": s["id"], "key": s["attached_product_key"]})
            if row is not None:
                done.append(str(dict(row)["id"]))
        expected = sorted(s["id"] for s in manifest["seeds"])
        if sorted(done) != expected:
            # Raising inside the transaction rolls every seed back: all-or-nothing.
            raise RuntimeError(f"deactivated {sorted(done)}, planned {expected} -- rolled back")
    print(f"applied: {len(done)} seed(s) inactive  run_id={manifest['run_id']}")
    return 0


async def revert(database: Any, manifest: Dict[str, Any]) -> int:
    restored: List[str] = []
    async with database.transaction():
        for s in manifest.get("seeds") or []:
            row = await database.fetch_one(
                REACTIVATE_SQL,
                {"id": s["id"], "key": s["attached_product_key"], "status": s["prior_status"]},
            )
            if row is not None:
                restored.append(str(dict(row)["id"]))
    skipped = sorted({s["id"] for s in manifest.get("seeds") or []} - set(restored))
    print(f"reverted run {manifest.get('run_id')}: {len(restored)} seed(s) restored; not inactive or re-pointed, left alone: {skipped}")
    return 0


async def run(args: argparse.Namespace) -> int:
    from db.database import database

    await database.connect()
    try:
        if args.command == "revert":
            if args.manifest_json:
                raw = args.manifest_json
            else:
                raw = Path(args.manifest).read_text()
            raw = raw.strip()
            if raw.startswith("MANIFEST "):
                raw = raw[len("MANIFEST "):]
            return await revert(database, json.loads(raw))
        return await plan_and_apply(database, apply=args.apply)
    finally:
        await database.disconnect()


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", nargs="?", default="plan", choices=["plan", "revert"])
    p.add_argument("--apply", action="store_true")
    p.add_argument("--manifest", help="revert: path to the manifest JSON")
    p.add_argument("--manifest-json", help="revert: the manifest JSON itself (the MANIFEST log line)")
    a = p.parse_args(argv)
    if a.command == "revert" and not (a.manifest or a.manifest_json):
        p.error("revert requires --manifest or --manifest-json")
    if a.command == "revert" and a.apply:
        p.error("--apply is for plan, not revert")
    return asyncio.run(run(a))


if __name__ == "__main__":
    raise SystemExit(main())
