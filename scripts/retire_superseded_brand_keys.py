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

ORDER. Run this AFTER the re-onboard has applied: a stale key is retired only once its NEW key is
live (review of #2397 -- the drain's re-run drops unresolved rows, excluded handles and refused plan
rows, and a job can sit held for review; measured 2026-09-27, 72 of stilacosmetics.com's 125 records
were unresolved, so retiring first would have hidden them). Both rows of a pair serving for a while is
the status quo; a missing product is not. --before-rewrite restores the old order explicitly.
Live is not served: a stale key whose old row is serving_eligible is also kept while its new key's
content_key is not (e.g. blocked on short_description) -- reported as NEW NOT SERVING, retired on a later
run once the new row serves.

(Formerly: run this BEFORE the re-onboard.) The two key sets are disjoint, so a suppressed
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

  # a store written under another spelling and re-run under the canonical one:
  DATABASE_URL=... python3 scripts/retire_superseded_brand_keys.py \
      --domain tartecosmetics.com --brand Tarte --stale-brand "Tarte Cosmetics" --category beauty

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
SELECT product_key, merchant_id, brand, title, source_domain, content_key, suppression_reason, suppressed_at,
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

# Serving is decided per content_key, not per product_key: a row is on the storefront only while its
# content_key is serving_eligible. A content_key with no state row, or a NULL flag, is not serving. The flag
# is read, not filtered on, so `plan` decides it in code the tests exercise.
SERVING_SQL = """
SELECT content_key, serving_eligible FROM index_pipeline_state
WHERE content_key = ANY(:keys)
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


async def build_cohort(domain: str, brand: str, category_path: str,
                       stale_brand: Optional[str] = None) -> List[Dict[str, Any]]:
    """(stale_key, new_key, brand, title) for every record the fix re-keyed.

    `emit_real_variants=True` mirrors the re-onboard invocation; it does not affect the
    key, but keeping the two calls identical is what makes the cohorts provably the same.

    `stale_brand`: the spelling the OLD rows were written under, when it is not the re-run's brand. A
    store once written as "Tarte Cosmetics" / "Stila Cosmetics" / "Tower 28 Beauty" and re-run as
    "Tarte" / "Stila" / "Tower 28" moves every key from derive(stale_brand, title) to
    derive(record brand, title); without it the stale key is derived from the re-run's own brand and
    the cohort comes back empty (the Sand & Sky case, 2026-09-26). Keys that were never written under
    the stale spelling are simply absent from catalog_products, and `plan` never retires an absent key.
    """
    recs = await records_for_brand(
        domain=domain, category_path=category_path, brand=brand, emit_real_variants=True
    )
    out: List[Dict[str, Any]] = []
    for rec in recs:
        pdp = rec.get("pdp") or {}
        title, new_brand = pdp.get("product_name"), pdp.get("brand")
        stale, new = derive_product_key(stale_brand or brand, title), derive_product_key(new_brand, title)
        if stale and new and stale != new:
            out.append({"stale_key": stale, "new_key": new, "brand": new_brand, "title": title})
    return out


def _host(value: Optional[str]) -> str:
    return str(value or "").strip().lower().removeprefix("www.")


def select_retirable(cohort: List[Dict[str, Any]], rows: Dict[str, Dict[str, Any]], new_live: set,
                     domain: str, *, serving: set, before_rewrite: bool = False) -> Dict[str, List[Dict[str, Any]]]:
    """Pure: split the cohort into what may be tombstoned and why the rest may not.

    A stale key is retirable only when it is present and live, owned by THIS store (derive_product_key
    hashes (brand, title) only, so the same key can belong to another source -- never touch it), and,
    unless before_rewrite, its new key is already live (the re-run actually wrote the product).

    `serving`: the product_keys (stale and new) whose content_key is serving_eligible. Required, so no
    caller can skip the check. A live new key is not enough when the old row is the one on the storefront:
    if the new row is blocked (short_description, ...) retiring the old row takes a served product off the
    catalog (hand-checked per store 2026-09-28). Such a key is kept as `new_not_serving`. An old row that is
    not serving loses nothing and is still retired. before_rewrite skips this with the new-key-live check:
    the old order accepts the gap explicitly."""
    host = _host(domain)
    present = [c for c in cohort if c["stale_key"] in rows]
    suppressed = [c for c in present if rows[c["stale_key"]].get("suppression_reason")]
    live = [c for c in present if c not in suppressed]
    foreign = [c for c in live if _host(rows[c["stale_key"]].get("source_domain")) != host]
    own = [c for c in live if c not in foreign]
    waiting = [] if before_rewrite else [c for c in own if c["new_key"] not in new_live]
    new_not_serving = [] if before_rewrite else [
        c for c in own if c not in waiting and c["stale_key"] in serving and c["new_key"] not in serving
    ]
    retire = [c for c in own if c not in waiting and c not in new_not_serving]
    return {"present": present, "live": retire, "foreign": foreign, "waiting_for_new_key": waiting,
            "new_not_serving": new_not_serving, "already_suppressed": suppressed}


async def load_serving(stale_rows: Dict[str, Dict[str, Any]], own_live_new_rows: List[Dict[str, Any]]) -> set:
    """The `serving` set for `select_retirable`: product_keys whose content_key is serving_eligible.

    One query over both sides' content_keys: the new key's serving state decides whether a retire loses a
    product, the old row's whether there is anything to lose. Pass only THIS store's live new rows -- a
    foreign or suppressed row under the new key is not the re-run's row and must not make it look served."""
    ck_of = {r["product_key"]: r.get("content_key")
             for r in [*stale_rows.values(), *own_live_new_rows] if r.get("content_key")}
    if not ck_of:
        return set()
    serving_cks = {
        r["content_key"] for r in await database.fetch_all(SERVING_SQL, {"keys": sorted(set(ck_of.values()))})
        if r["serving_eligible"] is True
    }
    return {k for k, ck in ck_of.items() if ck in serving_cks}


async def plan(domain: str, brand: str, category_path: str,
               stale_brand: Optional[str] = None, *, before_rewrite: bool = False) -> Dict[str, Any]:
    cohort = await build_cohort(domain, brand, category_path, stale_brand)
    stale_keys = [c["stale_key"] for c in cohort]
    new_keys = [c["new_key"] for c in cohort]
    rows = {r["product_key"]: dict(r) for r in await database.fetch_all(LIVE_ROWS_SQL, {"keys": stale_keys})}
    new_rows = [dict(r) for r in await database.fetch_all(LIVE_ROWS_SQL, {"keys": new_keys})]
    already_new = {r["product_key"] for r in new_rows}
    # A new key counts only when THIS store's re-run wrote it (live, own source_domain): another source's row
    # under the same (brand, title) key proves nothing about the re-run (re-review of #2397).
    new_live = {r["product_key"] for r in new_rows
                if not r.get("suppression_reason") and _host(r.get("source_domain")) == _host(domain)}
    serving = await load_serving(rows, [r for r in new_rows if r["product_key"] in new_live])
    split = select_retirable(cohort, rows, new_live, domain, serving=serving, before_rewrite=before_rewrite)
    present, live = split["present"], split["live"]
    # Seeds and offers for the keys this run will actually retire -- never a waiting or foreign key, so the
    # plan's counts are true and revert's manifest names only seeds this run deactivates.
    retire_keys = [c["stale_key"] for c in live]
    seeds = [dict(r) for r in await database.fetch_all(SEEDS_FOR_KEYS_SQL, {"keys": retire_keys})] if retire_keys else []
    offers = await cascade_for_suppressed_product_keys(retire_keys, apply=False) if retire_keys else []
    return {
        "foreign": split["foreign"], "waiting_for_new_key": split["waiting_for_new_key"],
        "new_not_serving": split["new_not_serving"], "already_suppressed": split["already_suppressed"],
        "domain": domain, "brand_override": brand, "category_path": category_path, "stale_brand": stale_brand,
        "cohort": cohort, "rows": rows, "present": present, "live": live,
        "already_new": sorted(already_new), "seeds": seeds,
        "active_seeds": [s for s in seeds if str(s.get("status") or "").lower() == "active"],
        "offers": offers,
    }


def print_plan(p: Dict[str, Any]) -> None:
    print(f"domain            : {p['domain']}  (brand override {p['brand_override']!r})")
    if p.get("stale_brand"):
        print(f"stale spelling    : {p['stale_brand']!r}  (old rows' keys derive from it)")
    print(f"re-keyed          : {len(p['cohort'])}")
    print(f"  present in catalog_products : {len(p['present'])}")
    print(f"  LIVE (would be tombstoned)  : {len(p['live'])}")
    print(f"  WAITING (new key not live)  : {len(p.get('waiting_for_new_key') or [])}  -- never retired")
    print(f"  NEW NOT SERVING (old served): {len(p.get('new_not_serving') or [])}  -- never retired")
    print(f"  FOREIGN (another source)    : {len(p.get('foreign') or [])}  -- never retired")
    print(f"  already suppressed          : {len(p.get('already_suppressed') or [])}")
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
        "run_id": run_id, "reason": REASON,
        "pr": "pivota-backend#2173" if not p.get("stale_brand") else "stale-brand re-key",
        "domain": p["domain"], "brand_override": p["brand_override"], "stale_brand": p.get("stale_brand"),
        "note": ("product_key superseded when the brand override stopped relabelling a sibling brand"
                 if not p.get("stale_brand") else
                 f"product_key superseded: rows written as {p['stale_brand']!r} re-run as {p['brand_override']!r}"),
        "at": datetime.now(timezone.utc).isoformat(),
    })
    # BEFORE-state first: a manifest written after the write cannot describe what it replaced.
    manifest = {
        "run_id": run_id, "reason": REASON, "domain": p["domain"],
        "brand_override": p["brand_override"], "category_path": p["category_path"],
        "stale_brand": p.get("stale_brand"),
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
        p = await plan(args.domain, args.brand, args.category, args.stale_brand,
                       before_rewrite=args.before_rewrite)
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
    p.add_argument("--before-rewrite", dest="before_rewrite", action="store_true",
                   help="retire stale keys even when their new key is not live yet (the old order)")
    p.add_argument("--stale-brand", dest="stale_brand",
                   help="the spelling the OLD rows were written under, when the re-run uses another")
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
