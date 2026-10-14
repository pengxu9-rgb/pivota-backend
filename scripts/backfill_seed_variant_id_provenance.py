#!/usr/bin/env python3
"""Stamp `variant_id_provenance` on enrichment-lane seed variants written before the lane stamped it.

WHY. The nightly seed refresh (routes/employee_products.py) keeps a seed's stored variants against a page read
only when they carry `variant_id_provenance == "merchant_issued"` (or the shade fold's own `url_source`). The
lane's seed writer (`services/catalog_enrichment_agent/ingestion._build_seed_inserts`) stamps that key only since
2026-09-08 (#2123); real variants it wrote from 2026-09-04 (the shade fold) to then carry the merchant's ids and
no stamp, so the refresh judges them by the old "richer read wins" rule until the listing is re-ingested.
`scripts/backfill_variant_id_provenance_stamps.py` stamps `catalog_skus.sku_payload` only, never
`seed_data.variants`.

THE RULE IS THE WRITER'S, FROM THE WRITER'S OWN INPUTS -- never a guess from the id's shape. The writer calls

    variant_id_provenance(vid, product_id=canonical_product_name(brand, product_name),
                          product_key=<product_key>, handle=v["source_handle"])

The seed row keeps three of those inputs (`seed_data.brand`, `seed_data.product_name`, `attached_product_key`)
but not the handle. The same ingest wrote one `catalog_skus` row per real variant (`_build_variant_sku_inserts`,
since 2026-09-04) whose `sku_payload` records `agent_version`, the full `variant_id` and the `source_handle` the
writer classified with. That row is the ATTESTATION: a seed variant is stamped only when a lane SKU row of the
same product names the same id and records its handle. Everything else is REPORTED, never written:
  * `unattested`         -- no lane SKU row names this id. A page read's id (a merchant SKU code, a GTIN the
                            refresh's JSON-LD extractor reads as sku/gtin13) lands here: digits alone are not
                            the merchant's variant identity, and stamping one `merchant_issued` would freeze a
                            read the refresh is meant to replace;
  * `handle_unrecorded`  -- the SKU row predates `source_handle` in its payload, so the 4th input is unknown;
  * `ambiguous`          -- two lane SKU rows (two sellers) record handles that classify the id differently;
  * `sku_stamp_disagrees`-- the SKU row already carries a stamp and today's call disagrees with it;
  * `no_product_name`    -- the seed lacks the brand/product name the writer's product_id is built from.
The writer's lone synthetic `<external_product_id>::canonical` variant is out of scope (its stamp is a literal,
and no refresh guard reads it). An existing stamp is never touched, and suppressed/inactive seeds are not scanned.

WHAT IT WRITES. Only the key `variant_id_provenance` on the planned variants of `seed_data.variants`, in SQL
(`jsonb || {key: value}` per element, order kept), so price/stock/url bytes are never round-tripped through Python.
`updated_at` is NOT bumped: a stamp is not a new observation of the page. No serving view projects this key, so
there is nothing to rebuild.

DRIFT GUARD. The plan records `md5(seed_data->'variants')` per seed. The write requires it unchanged, and any
seed changed since the plan (a refresh, a re-ingest) aborts the whole transaction. The manifest is STORED in
identity_resolution_events before the write; the applied event records each seed's post-write fingerprint, and a
revert removes only this run's stamps from seeds still at that fingerprint (a seed rewritten since keeps whatever
its writer put there, reported as `changed_since_apply`).

  Dry run (default; READ ONLY transaction, statement_timeout 30s; writes nothing):
    python -m scripts.backfill_seed_variant_id_provenance
  Apply (manifest stored before the write; one transaction):
    python -m scripts.backfill_seed_variant_id_provenance --apply
  Revert a run (refused while a LATER applied run holds any of its seeds):
    python -m scripts.backfill_seed_variant_id_provenance revert --run-id seedprov_<hex>

Needs DATABASE_URL: run it through scripts/ops/run_oneoff_job.sh.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from collections import Counter
from typing import Any, Dict, List, Mapping, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.database import database  # noqa: E402
from services.catalog_enrichment_agent.ingestion import (  # noqa: E402
    AGENT_VERSION,
    SYNTHETIC_PLATFORM,
    canonical_product_name,
)
from services.variant_identity import MERCHANT_ISSUED, variant_id_provenance  # noqa: E402

LANE = AGENT_VERSION
STAMP = "variant_id_provenance"
PAGE = 500
SAMPLE = 20

# Active lane seeds with at least one unstamped variant that is not the writer's synthetic canonical.
CANDIDATES_SQL = """
SELECT s.id, s.external_product_id, s.attached_product_key,
       s.seed_data->>'brand' AS brand, s.seed_data->>'product_name' AS product_name,
       s.seed_data->'variants' AS variants, md5((s.seed_data->'variants')::text) AS fp
FROM external_product_seeds s
WHERE s.id > :after AND s.tool = :lane AND s.status = 'active' AND s.attached_product_key IS NOT NULL
  AND jsonb_typeof(s.seed_data->'variants') = 'array'
  AND EXISTS (
    SELECT 1 FROM jsonb_array_elements(s.seed_data->'variants') v
    WHERE jsonb_typeof(v) = 'object' AND (v->'variant_id_provenance') IS NULL
      AND coalesce(btrim(v->>'variant_id'), '') <> ''
      AND v->>'variant_id' IS DISTINCT FROM (s.external_product_id || '::canonical'))
ORDER BY s.id
LIMIT :page
"""

# The writer's own variant SKU rows for these products (`_build_variant_sku_inserts`): the attestation.
WRITER_SKUS_SQL = """
SELECT k.product_key, k.sku_payload->>'variant_id' AS variant_id,
       (k.sku_payload->'source_handle') IS NOT NULL AS handle_recorded,
       k.sku_payload->>'source_handle' AS source_handle,
       k.sku_payload->>'variant_id_provenance' AS sku_stamp
FROM catalog_skus k
WHERE k.product_key = ANY(:keys) AND k.platform = :platform
  AND jsonb_typeof(k.sku_payload) = 'object' AND k.sku_payload->>'agent_version' = :lane
"""

# One element-wise rebuild of `seed_data.variants`, order kept; the stamps are joined in as a typed recordset
# (a bind inside jsonb_build_object is untyped and fails at PREPARE, #1703). `{element}` is the per-element edit.
_REBUILD_SQL = """
UPDATE external_product_seeds s
SET seed_data = jsonb_set(s.seed_data, '{{variants}}', COALESCE((
  SELECT jsonb_agg({element} ORDER BY e.ord)
  FROM jsonb_array_elements(s.seed_data->'variants') WITH ORDINALITY e(v, ord)
  LEFT JOIN jsonb_to_recordset(CAST(:stamps AS jsonb)) st(i int, variant_id text, provenance text)
    ON st.i = e.ord - 1 AND st.variant_id = e.v->>'variant_id'
), s.seed_data->'variants'))
WHERE s.id = :id AND md5((s.seed_data->'variants')::text) = :fp
RETURNING md5((seed_data->'variants')::text) AS fp_after
"""
# Adds the key only where it is still absent.
STAMP_SQL = _REBUILD_SQL.format(element=(
    "CASE WHEN st.i IS NOT NULL AND (e.v->'variant_id_provenance') IS NULL "
    "THEN e.v || jsonb_build_object('variant_id_provenance', st.provenance) ELSE e.v END"))
# Removes it only where it still reads this run's value.
UNSTAMP_SQL = _REBUILD_SQL.format(element=(
    "CASE WHEN st.i IS NOT NULL AND e.v->>'variant_id_provenance' = st.provenance "
    "THEN e.v - 'variant_id_provenance' ELSE e.v END"))

EVENT_SQL = """
INSERT INTO identity_resolution_events (proposal_id, action, run_id, detail)
VALUES (NULL, :action, :run_id, CAST(:detail AS jsonb))
RETURNING id
"""
RUN_STATE_SQL = """
SELECT action, id FROM identity_resolution_events WHERE run_id = :run_id AND action = ANY(:actions)
"""
LOAD_EVENT_SQL = """
SELECT detail FROM identity_resolution_events WHERE action = :action AND run_id = :run_id
ORDER BY id DESC LIMIT 1
"""
LATER_LIVE_RUNS_SQL = """
SELECT a.run_id FROM identity_resolution_events a
WHERE a.action = :applied AND a.id > :after_id
  AND NOT EXISTS (SELECT 1 FROM identity_resolution_events r WHERE r.run_id = a.run_id AND r.action = :reverted)
"""

MANIFEST_ACTION = "seed_variant_provenance_backfill_manifest"
APPLIED_ACTION = "seed_variant_provenance_backfill_applied"
REVERTED_ACTION = "seed_variant_provenance_backfill_reverted"


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def verdict_for(variant_id: str, *, brand: Optional[str], product_name: Optional[str], product_key: str,
                writer_rows: List[Mapping[str, Any]]) -> Dict[str, Any]:
    """The writer's call for one seed variant id, or the reason it cannot be reproduced."""
    if not writer_rows:
        return {"outcome": "unattested"}
    if not (brand or "").strip() or not (product_name or "").strip():
        return {"outcome": "no_product_name"}
    if not all(r["handle_recorded"] for r in writer_rows):
        return {"outcome": "handle_unrecorded"}
    product_id = canonical_product_name(brand, product_name)
    verdicts = {variant_id_provenance(variant_id, product_id=product_id, product_key=product_key,
                                      handle=r["source_handle"]) for r in writer_rows}
    if len(verdicts) != 1:
        return {"outcome": "ambiguous", "verdicts": sorted(verdicts)}
    (verdict,) = verdicts
    sku_stamps = {r["sku_stamp"] for r in writer_rows if r["sku_stamp"]}
    if sku_stamps and sku_stamps != {verdict}:
        return {"outcome": "sku_stamp_disagrees", "verdict": verdict, "sku_stamps": sorted(sku_stamps)}
    return {"outcome": "stamp", "provenance": verdict}


def plan_seed(seed: Mapping[str, Any], writer_rows: List[Mapping[str, Any]]) -> Dict[str, Any]:
    by_id: Dict[str, List[Mapping[str, Any]]] = {}
    for r in writer_rows:
        by_id.setdefault(str(r["variant_id"] or ""), []).append(r)
    stamps: List[Dict[str, Any]] = []
    refused: List[Dict[str, Any]] = []
    for i, v in enumerate(_json(seed["variants"]) or []):
        if not isinstance(v, dict) or STAMP in v:
            continue
        vid = str(v.get("variant_id") or "").strip()
        if not vid:
            continue
        if v["variant_id"] != vid:
            # The writer stores the stripped string it classified; anything else is not its spelling.
            refused.append({"i": i, "variant_id": vid, "outcome": "id_not_verbatim"})
            continue
        verdict = verdict_for(vid, brand=seed.get("brand"), product_name=seed.get("product_name"),
                              product_key=seed["attached_product_key"], writer_rows=by_id.get(vid, []))
        if verdict["outcome"] == "stamp":
            stamps.append({"i": i, "variant_id": vid, "provenance": verdict["provenance"]})
        else:
            refused.append({"i": i, "variant_id": vid, **verdict})
    return {"seed_id": seed["id"], "fp": seed["fp"], "stamps": stamps, "refused": refused}


async def plan(db: Any, *, page: int = PAGE) -> Dict[str, Any]:
    seeds: List[Dict[str, Any]] = []
    after = ""
    scanned = 0
    while True:
        rows = [dict(r) for r in await db.fetch_all(CANDIDATES_SQL, {"after": after, "lane": LANE, "page": page})]
        if not rows:
            break
        scanned += len(rows)
        after = rows[-1]["id"]
        keys = sorted({r["attached_product_key"] for r in rows})
        writers: Dict[str, List[Dict[str, Any]]] = {}
        for k in await db.fetch_all(WRITER_SKUS_SQL, {"keys": keys, "platform": SYNTHETIC_PLATFORM, "lane": LANE}):
            writers.setdefault(k["product_key"], []).append(dict(k))
        for r in rows:
            seeds.append(plan_seed(r, writers.get(r["attached_product_key"], [])))
        if len(rows) < page:
            break
    return {"scanned": scanned, "seeds": seeds}


def summary(p: Mapping[str, Any]) -> Dict[str, Any]:
    seeds = p["seeds"]
    stamping = [s for s in seeds if s["stamps"]]
    by_verdict = Counter(st["provenance"] for s in seeds for st in s["stamps"])
    refused = Counter(r["outcome"] for s in seeds for r in s["refused"])
    refused_numeric = Counter(r["outcome"] for s in seeds for r in s["refused"]
                              if r["variant_id"].isdigit() and len(r["variant_id"]) >= 8)
    return {
        "candidate_seeds": p["scanned"],
        "seeds_to_stamp": len(stamping),
        "seeds_gaining_merchant_issued": sum(
            1 for s in stamping if any(st["provenance"] == MERCHANT_ISSUED for st in s["stamps"])),
        "variants_to_stamp": sum(len(s["stamps"]) for s in seeds),
        "variants_to_stamp_by_verdict": dict(by_verdict),
        "variants_refused": dict(refused),
        "variants_refused_numeric_8plus": dict(refused_numeric),
        "seeds_fully_refused": sum(1 for s in seeds if s["refused"] and not s["stamps"]),
        "stamp_sample": [{"seed_id": s["seed_id"], "stamps": s["stamps"][:3]} for s in stamping[:SAMPLE]],
        "refused_sample": [{"seed_id": s["seed_id"], **r} for s in seeds for r in s["refused"]][:SAMPLE],
    }


async def _record(db: Any, action: str, run_id: str, detail: Mapping[str, Any]) -> None:
    row = await db.fetch_one(EVENT_SQL, {"action": action, "run_id": run_id,
                                         "detail": json.dumps(detail, default=str)})
    if not row:
        raise RuntimeError(f"{action} for {run_id} was not recorded")


async def _load(db: Any, action: str, run_id: str) -> Dict[str, Any]:
    row = await db.fetch_one(LOAD_EVENT_SQL, {"action": action, "run_id": run_id})
    detail = _json(row and row["detail"])
    if not detail:
        raise SystemExit(f"no stored {action} for {run_id}")
    return detail


async def dry_run(db: Any) -> Dict[str, Any]:
    """The plan inside a READ ONLY transaction with its own statement timeout: a dry run cannot write."""
    async with db.transaction():
        await db.execute("SET TRANSACTION READ ONLY")
        await db.execute("SET LOCAL statement_timeout = '30s'")
        return await plan(db)


async def apply(db: Any, p: Mapping[str, Any]) -> Dict[str, Any]:
    seeds = [{"seed_id": s["seed_id"], "fp": s["fp"], "stamps": s["stamps"]} for s in p["seeds"] if s["stamps"]]
    if not seeds:
        raise SystemExit("nothing to stamp")
    run_id = f"seedprov_{uuid.uuid4().hex[:12]}"
    await _record(db, MANIFEST_ACTION, run_id, {"run_id": run_id, "seeds": seeds})  # committed BEFORE the write
    after: Dict[str, str] = {}
    async with db.transaction():
        for s in seeds:
            row = await db.fetch_one(STAMP_SQL, {"id": s["seed_id"], "fp": s["fp"],
                                                 "stamps": json.dumps(s["stamps"])})
            if not row:
                raise RuntimeError(f"drift: seed {s['seed_id']} changed since the plan; nothing written")
            after[s["seed_id"]] = row["fp_after"]
        await _record(db, APPLIED_ACTION, run_id, {"seeds": len(after), "fp_after": after,
                                                   "variants": sum(len(s["stamps"]) for s in seeds)})
    print(f"COMMITTED {run_id}: {sum(len(s['stamps']) for s in seeds)} variant(s) stamped on {len(after)} seed(s)",
          flush=True)
    return {"run_id": run_id, "seeds": len(after), "variants": sum(len(s["stamps"]) for s in seeds)}


async def revert(db: Any, run_id: str) -> Dict[str, Any]:
    m = await _load(db, MANIFEST_ACTION, run_id)
    state = {r["action"]: r["id"] for r in await db.fetch_all(
        RUN_STATE_SQL, {"run_id": run_id, "actions": [APPLIED_ACTION, REVERTED_ACTION]})}
    if APPLIED_ACTION not in state:
        raise SystemExit(f"{run_id} never committed: nothing to revert")
    if REVERTED_ACTION in state:
        raise SystemExit(f"{run_id} is already reverted")
    mine = {s["seed_id"] for s in m["seeds"]}
    for r in await db.fetch_all(LATER_LIVE_RUNS_SQL, {"applied": APPLIED_ACTION, "reverted": REVERTED_ACTION,
                                                      "after_id": state[APPLIED_ACTION]}):
        later = await _load(db, MANIFEST_ACTION, r["run_id"])
        shared = mine & {s["seed_id"] for s in later["seeds"]}
        if shared:
            raise SystemExit(f"refused: later run {r['run_id']} stamped {len(shared)} of these seeds; revert it first")
    fp_after = (await _load(db, APPLIED_ACTION, run_id))["fp_after"]
    reverted: List[str] = []
    changed: List[str] = []
    async with db.transaction():
        for s in m["seeds"]:
            row = await db.fetch_one(UNSTAMP_SQL, {"id": s["seed_id"], "fp": fp_after.get(s["seed_id"]),
                                                   "stamps": json.dumps(s["stamps"])})
            (reverted if row else changed).append(s["seed_id"])
        done = {"reverted": len(reverted), "changed_since_apply": len(changed),
                "changed_sample": changed[:SAMPLE]}
        await _record(db, REVERTED_ACTION, run_id, done)
    print(f"REVERTED {run_id}: {len(reverted)} seed(s); {len(changed)} rewritten since the apply, left as they are",
          flush=True)
    return {"run_id": run_id, **done}


async def run(args: argparse.Namespace) -> int:
    await database.connect()
    try:
        if args.command == "revert":
            print("REVERT_DONE " + json.dumps(await revert(database, args.run_id), default=str), flush=True)
            return 0
        p = await (plan(database) if args.apply else dry_run(database))
        print("PLAN " + json.dumps(summary(p), default=str), flush=True)
        if args.apply:
            print("APPLIED " + json.dumps(await apply(database, p), default=str), flush=True)
        else:
            print("dry run: nothing written (--apply to write)", flush=True)
        return 0
    finally:
        await database.disconnect()


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", nargs="?", default="plan", choices=["plan", "revert"])
    p.add_argument("--apply", action="store_true")
    p.add_argument("--run-id", dest="run_id")
    args = p.parse_args(argv)
    if args.command == "revert" and not args.run_id:
        p.error("revert needs --run-id")
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
