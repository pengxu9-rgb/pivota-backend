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
  * `no_product_name`    -- the seed lacks the brand/product name the writer's product_id is built from;
  * `id_not_verbatim`    -- the stored id is not the stripped string the writer stores.
The writer's synthetic `...::canonical` variant is skipped, not refused (its stamp is a literal, and no refresh
guard reads it). An existing stamp is never touched, and suppressed/inactive seeds are not scanned.

WHAT A STAMP FREEZES, AND WHAT IS HELD BACK FOR IT. Once stamped `merchant_issued`, a variant can no longer be
dropped by #2601's refresh. These seeds have not been rewritten by the drain since 2026-09-08, so nothing will
correct a wrong stamp. The known way to get one wrong is a www/apex duplicate: #2593 leaves the seeds of both
spellings of one listing live, and stamping the orphaned spelling freezes a duplicate that should be retired. So
a candidate with an ACTIVE lane sibling seed of the same product key and the same listing spelled with or
without `www.` is HELD (`held_www_sibling`), whether or not that sibling is stamped. The report splits held
seeds by the sibling's state. `--include-www-siblings` releases them. A stamp whose id equals its SKU row's `sku`
or `barcode` (a GTIN or SKU code doing duty as the variant id) is reported per host, not held: the writer
itself classified that id.

WHAT IT WRITES. Only the key `variant_id_provenance` on the planned variants of `seed_data.variants`, in SQL
(`jsonb || {key: value}` per element, order kept), so price/stock/url bytes are never round-tripped through Python.
`updated_at` is NOT bumped: a stamp is not a new observation of the page. No serving view projects this key, so
there is nothing to rebuild.

BOUND TO THE REVIEWED DRY RUN. The plan, under --apply too, runs in a READ ONLY transaction. It prints
`plan_digest`, a sha256 of exactly the stamps it would write (seed, index, id, verdict; NOT the seed's bytes, so
a nightly price refresh does not invalidate a review). --apply refuses to write, and records nothing, unless
`--expect-digest` equals the digest it re-plans. Caps (`--hosts`, `--seed-ids`, `--max-seeds`) narrow the plan
before the digest, so a canary's dry run and its apply carry the same caps.

DRIFT GUARD. The plan records `md5(seed_data->'variants')` per seed. The write requires it unchanged, and any
seed changed since the plan (a refresh, a re-ingest) aborts the whole transaction. The write and the revert run
under `lock_timeout` and `statement_timeout`, so a row lock held by a refresh fails the run instead of queueing it
behind that lock while it holds its own. The manifest is STORED in identity_resolution_events before the write;
the applied event records each seed's post-write fingerprint, and a revert removes only this run's stamps from
seeds still at that fingerprint (a seed rewritten since keeps whatever its writer put there, reported as
`changed_since_apply`).

  Dry run (default; READ ONLY transaction, statement_timeout 30s; writes nothing):
    python -m scripts.backfill_seed_variant_id_provenance [--hosts a.example b.example] [--max-seeds 10]
  Apply exactly the reviewed plan (manifest stored before the write; one transaction):
    python -m scripts.backfill_seed_variant_id_provenance --apply --expect-digest <plan_digest> [same caps]
  Revert a run (refused while a LATER applied run holds any of its seeds):
    python -m scripts.backfill_seed_variant_id_provenance revert --run-id seedprov_<hex>

Needs DATABASE_URL: run it through scripts/ops/run_oneoff_job.sh.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import uuid
from collections import Counter, defaultdict
from typing import Any, Dict, Iterable, List, Mapping, Optional
from urllib.parse import urlsplit

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
SYNTHETIC_SUFFIX = "::canonical"
LOCK_TIMEOUT = "5s"
STATEMENT_TIMEOUT = "30s"

# Active lane seeds with at least one unstamped variant that is not the writer's synthetic canonical.
CANDIDATES_SQL = """
SELECT s.id, s.external_product_id, s.attached_product_key, s.domain, s.canonical_url, s.destination_url,
       s.seed_data->>'brand' AS brand, s.seed_data->>'product_name' AS product_name,
       s.seed_data->'variants' AS variants, md5((s.seed_data->'variants')::text) AS fp
FROM external_product_seeds s
WHERE s.id > :after AND s.tool = :lane AND s.status = 'active' AND s.attached_product_key IS NOT NULL
  AND jsonb_typeof(s.seed_data->'variants') = 'array'
  AND EXISTS (
    SELECT 1 FROM jsonb_array_elements(s.seed_data->'variants') v
    WHERE jsonb_typeof(v) = 'object' AND (v->'variant_id_provenance') IS NULL
      AND coalesce(btrim(v->>'variant_id'), '') <> ''
      AND right(v->>'variant_id', 11) <> '::canonical')
ORDER BY s.id
LIMIT :page
"""

# The writer's own variant SKU rows for these products (`_build_variant_sku_inserts`): the attestation.
WRITER_SKUS_SQL = """
SELECT k.product_key, k.sku_payload->>'variant_id' AS variant_id,
       (k.sku_payload->'source_handle') IS NOT NULL AS handle_recorded,
       k.sku_payload->>'source_handle' AS source_handle,
       k.sku_payload->>'variant_id_provenance' AS sku_stamp, k.sku, k.barcode
FROM catalog_skus k
WHERE k.product_key = ANY(:keys) AND k.platform = :platform
  AND jsonb_typeof(k.sku_payload) = 'object' AND k.sku_payload->>'agent_version' = :lane
"""

# Every active lane seed of these products, for the www/apex sibling hold.
SIBLINGS_SQL = """
SELECT s.id, s.attached_product_key, s.canonical_url, s.destination_url,
       EXISTS (SELECT 1 FROM jsonb_array_elements(
                 CASE WHEN jsonb_typeof(s.seed_data->'variants') = 'array' THEN s.seed_data->'variants'
                      ELSE '[]'::jsonb END) v
               WHERE jsonb_typeof(v) = 'object' AND (v->'variant_id_provenance') IS NOT NULL) AS stamped
FROM external_product_seeds s
WHERE s.attached_product_key = ANY(:keys) AND s.tool = :lane AND s.status = 'active'
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


def _host(url: Any) -> str:
    try:
        return (urlsplit(str(url or "")).hostname or "").lower()
    except ValueError:
        return ""


def _bare(host: str) -> str:
    return host[4:] if host.startswith("www.") else host


def listing_key(row: Mapping[str, Any]) -> Optional[tuple]:
    """One listing whichever way its host is spelled (www vs apex): bare host + path, no query or slash."""
    url = row.get("canonical_url") or row.get("destination_url")
    host = _host(url)
    if not host:
        return None
    return (_bare(host), urlsplit(str(url)).path.rstrip("/").lower())


def seed_host(row: Mapping[str, Any]) -> str:
    return _bare((str(row.get("domain") or "").strip().lower()) or _host(row.get("canonical_url")
                                                                          or row.get("destination_url")))


def plan_seed(seed: Mapping[str, Any], writer_rows: List[Mapping[str, Any]]) -> Dict[str, Any]:
    by_id: Dict[str, List[Mapping[str, Any]]] = {}
    for r in writer_rows:
        by_id.setdefault(str(r["variant_id"] or ""), []).append(r)
    stamps: List[Dict[str, Any]] = []
    refused: List[Dict[str, Any]] = []
    skipped_synthetic = 0
    id_is_sku_or_barcode: List[str] = []
    for i, v in enumerate(_json(seed["variants"]) or []):
        if not isinstance(v, dict) or STAMP in v:
            continue
        vid = str(v.get("variant_id") or "").strip()
        if not vid:
            continue
        if vid.endswith(SYNTHETIC_SUFFIX):
            skipped_synthetic += 1
            continue
        if v["variant_id"] != vid:
            # The writer stores the stripped string it classified; anything else is not its spelling.
            refused.append({"i": i, "variant_id": vid, "outcome": "id_not_verbatim"})
            continue
        rows = by_id.get(vid, [])
        verdict = verdict_for(vid, brand=seed.get("brand"), product_name=seed.get("product_name"),
                              product_key=seed["attached_product_key"], writer_rows=rows)
        if verdict["outcome"] == "stamp":
            stamps.append({"i": i, "variant_id": vid, "provenance": verdict["provenance"]})
            if any(vid in {str(r.get("sku") or "").strip(), str(r.get("barcode") or "").strip()} for r in rows):
                id_is_sku_or_barcode.append(vid)
        else:
            refused.append({"i": i, "variant_id": vid, **verdict})
    return {"seed_id": seed["id"], "host": seed_host(seed), "fp": seed["fp"], "stamps": stamps, "refused": refused,
            "held": [], "held_reason": None, "skipped_synthetic": skipped_synthetic,
            "id_is_sku_or_barcode": id_is_sku_or_barcode}


def _hold(seed: Dict[str, Any], reason: str) -> None:
    seed["held"], seed["stamps"], seed["held_reason"] = seed["stamps"], [], reason


async def plan(db: Any, *, page: int = PAGE, hosts: Optional[Iterable[str]] = None,
               seed_ids: Optional[Iterable[str]] = None, max_seeds: Optional[int] = None,
               include_www_siblings: bool = False) -> Dict[str, Any]:
    want_hosts = {_bare(h.strip().lower()) for h in hosts or [] if h.strip()}
    want_ids = {i.strip() for i in seed_ids or [] if i.strip()}
    seeds: List[Dict[str, Any]] = []
    after = ""
    scanned = 0
    while True:
        rows = [dict(r) for r in await db.fetch_all(CANDIDATES_SQL, {"after": after, "lane": LANE, "page": page})]
        if not rows:
            break
        after = rows[-1]["id"]
        full_page = len(rows) == page
        rows = [r for r in rows if (not want_hosts or seed_host(r) in want_hosts)
                and (not want_ids or r["id"] in want_ids)]
        scanned += len(rows)
        keys = sorted({r["attached_product_key"] for r in rows})
        writers: Dict[str, List[Dict[str, Any]]] = {}
        siblings: Dict[str, List[Dict[str, Any]]] = {}
        if keys:
            for k in await db.fetch_all(WRITER_SKUS_SQL, {"keys": keys, "platform": SYNTHETIC_PLATFORM,
                                                          "lane": LANE}):
                writers.setdefault(k["product_key"], []).append(dict(k))
            for k in await db.fetch_all(SIBLINGS_SQL, {"keys": keys, "lane": LANE}):
                siblings.setdefault(k["attached_product_key"], []).append(dict(k))
        for r in rows:
            seed = plan_seed(r, writers.get(r["attached_product_key"], []))
            mine = listing_key(r)
            twins = [t for t in siblings.get(r["attached_product_key"], [])
                     if t["id"] != r["id"] and mine is not None and listing_key(t) == mine]
            seed["www_siblings"] = [{"id": t["id"], "stamped": bool(t["stamped"])} for t in twins]
            if twins and seed["stamps"] and not include_www_siblings:
                _hold(seed, "www_sibling_stamped" if any(t["stamped"] for t in twins) else "www_sibling_unstamped")
            seeds.append(seed)
        if not full_page:
            break
    if max_seeds is not None:
        kept = 0
        for seed in seeds:
            if seed["stamps"]:
                if kept >= max_seeds:
                    _hold(seed, "max_seeds")
                else:
                    kept += 1
    return {"scanned": scanned, "seeds": seeds}


def plan_digest(p: Mapping[str, Any]) -> str:
    """Exactly the stamps the plan would write -- not the seeds' bytes, which a price refresh moves nightly."""
    planned = sorted((s["seed_id"], st["i"], st["variant_id"], st["provenance"])
                     for s in p["seeds"] for st in s["stamps"])
    return hashlib.sha256(json.dumps(planned, separators=(",", ":")).encode()).hexdigest()


def summary(p: Mapping[str, Any]) -> Dict[str, Any]:
    seeds = p["seeds"]
    stamping = [s for s in seeds if s["stamps"]]
    by_verdict = Counter(st["provenance"] for s in seeds for st in s["stamps"])
    refused = Counter(r["outcome"] for s in seeds for r in s["refused"])
    refused_numeric = Counter(r["outcome"] for s in seeds for r in s["refused"]
                              if r["variant_id"].isdigit() and len(r["variant_id"]) >= 8)
    by_host: Dict[str, Counter] = defaultdict(Counter)
    for s in seeds:
        c = by_host[s["host"]]
        c["candidate_seeds"] += 1
        if s["stamps"]:
            c["seeds_to_stamp"] += 1
            c["variants_to_stamp"] += len(s["stamps"])
        if s["held"]:
            c[f"seeds_held_{s['held_reason']}"] += 1
        c["variants_refused"] += len(s["refused"])
        c["stamps_id_is_sku_or_barcode"] += len(s["id_is_sku_or_barcode"]) if s["stamps"] else 0
    held = [s for s in seeds if s["held"]]
    return {
        "plan_digest": plan_digest(p),
        "candidate_seeds": p["scanned"],
        "seeds_to_stamp": len(stamping),
        "seeds_gaining_merchant_issued": sum(
            1 for s in stamping if any(st["provenance"] == MERCHANT_ISSUED for st in s["stamps"])),
        "variants_to_stamp": sum(len(s["stamps"]) for s in seeds),
        "variants_to_stamp_by_verdict": dict(by_verdict),
        "seeds_held": dict(Counter(s["held_reason"] for s in held)),
        "variants_held": sum(len(s["held"]) for s in held),
        "variants_refused": dict(refused),
        "variants_refused_numeric_8plus": dict(refused_numeric),
        "variants_skipped_synthetic": sum(s["skipped_synthetic"] for s in seeds),
        "stamps_id_is_sku_or_barcode": sum(len(s["id_is_sku_or_barcode"]) for s in stamping),
        "seeds_fully_refused": sum(1 for s in seeds if s["refused"] and not s["stamps"] and not s["held"]),
        "by_host": {h: dict(c) for h, c in sorted(by_host.items(), key=lambda kv: -kv[1]["candidate_seeds"])},
        "stamp_sample": [{"seed_id": s["seed_id"], "stamps": s["stamps"][:3]} for s in stamping[:SAMPLE]],
        "held_sample": [{"seed_id": s["seed_id"], "reason": s["held_reason"], "www_siblings": s["www_siblings"]}
                        for s in held if s["held_reason"] != "max_seeds"][:SAMPLE],
        "id_is_sku_or_barcode_sample": [{"seed_id": s["seed_id"], "ids": s["id_is_sku_or_barcode"][:3]}
                                        for s in stamping if s["id_is_sku_or_barcode"]][:SAMPLE],
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


async def dry_run(db: Any, **caps: Any) -> Dict[str, Any]:
    """The plan inside a READ ONLY transaction with its own statement timeout: a dry run cannot write."""
    async with db.transaction():
        await db.execute("SET TRANSACTION READ ONLY")
        await db.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
        return await plan(db, **caps)


async def _bounded(db: Any) -> None:
    """Inside a write transaction: fail on a held row lock instead of queueing behind it holding our own."""
    await db.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
    await db.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")


async def apply(db: Any, p: Mapping[str, Any], *, expect_digest: Optional[str]) -> Dict[str, Any]:
    digest = plan_digest(p)
    if not expect_digest or expect_digest != digest:
        raise SystemExit(f"refused: --expect-digest {expect_digest!r} is not this plan's {digest}; review the dry run "
                         "with the same caps first. Nothing written.")
    seeds = [{"seed_id": s["seed_id"], "fp": s["fp"], "stamps": s["stamps"]} for s in p["seeds"] if s["stamps"]]
    if not seeds:
        raise SystemExit("nothing to stamp")
    run_id = f"seedprov_{uuid.uuid4().hex[:12]}"
    await _record(db, MANIFEST_ACTION, run_id, {"run_id": run_id, "plan_digest": digest,
                                                "seeds": seeds})  # committed BEFORE the write
    after: Dict[str, str] = {}
    async with db.transaction():
        await _bounded(db)
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
        await _bounded(db)
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
        p = await dry_run(database, hosts=args.hosts, seed_ids=args.seed_ids, max_seeds=args.max_seeds,
                          include_www_siblings=args.include_www_siblings)
        print("PLAN " + json.dumps(summary(p), default=str), flush=True)
        if args.apply:
            out = await apply(database, p, expect_digest=args.expect_digest)
            print("APPLIED " + json.dumps(out, default=str), flush=True)
        else:
            print("dry run: nothing written (--apply to write)", flush=True)
        return 0
    finally:
        await database.disconnect()


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", nargs="?", default="plan", choices=["plan", "revert"])
    p.add_argument("--apply", action="store_true")
    p.add_argument("--expect-digest", dest="expect_digest", help="apply: the reviewed dry run's plan_digest")
    p.add_argument("--hosts", nargs="+", help="only seeds on these hosts (www. ignored)")
    p.add_argument("--seed-ids", dest="seed_ids", nargs="+", help="only these seed ids")
    p.add_argument("--max-seeds", dest="max_seeds", type=int, help="stamp at most this many seeds (id order)")
    p.add_argument("--include-www-siblings", dest="include_www_siblings", action="store_true",
                   help="also stamp seeds that have an active www/apex sibling (held by default)")
    p.add_argument("--run-id", dest="run_id")
    args = p.parse_args(argv)
    if args.command == "revert" and not args.run_id:
        p.error("revert needs --run-id")
    if args.apply and not args.expect_digest:
        p.error("--apply needs --expect-digest (the reviewed dry run's plan_digest)")
    if args.max_seeds is not None and args.max_seeds < 1:
        p.error("--max-seeds must be >= 1")
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
