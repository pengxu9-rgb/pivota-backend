#!/usr/bin/env python3
"""Label enrichment-lane offers that were written before the lane labelled seller type.

WHY. `catalog_enrichment_agent` stamps each offer's seller type (`offer_type`, `is_first_party`) with
`services.offer_seller_identity.derive_offer_seller_identity` only since 2026-09-16 (#2185). Offers created before
then were written with `offer_type` NULL, and the upsert keeps an existing offer's label (COALESCE) -- so an offer
is labelled only if its listing is re-ingested. Census 2026-10-06 (prod, read-only): of the 2,380 PUBLIC canonical
enrichment listings, 1,787 have only unlabelled offers; for 787 of them the lane's own rule says `brand_direct`
today (e.g. MAC Cosmetics on maccosmetics.com), and 575 of those then pass every other check of the gateway's
current-own-money read (PIVOTA-Agent src/services/canonicalOwnOfferSellerSql.js), which refuses an unlabelled
brand-store offer. The buyable public product pages go from ~477 to ~1,050.

WHAT IT WRITES. Exactly what the lane would write on a re-ingest of the same listing, and nothing it would not:
  * the verdict is `derive_offer_seller_identity(domain=<listing source_domain>, canonical_url=<listing
    canonical_url>, brand=<listing brand>)` -- the call `ingestion._offer_seller_identity` makes from the same
    three fields of the listing's payload;
  * only `brand_direct` verdicts are written (`is_first_party` TRUE with it, as the lane writes them together).
    `unknown_no_evidence` stays unlabelled -- the rule refuses to guess, and so does this tool -- UNLESS an operator
    attested the listing's own domain as the official store of that offer's seller (merchant_domain_attestations,
    migration 259; never a `verified` proof). The attested domain is then the rule's `official_domain` input and
    the rule itself decides (official_domain_match; a known retailer host still wins); such labels are recorded
    with rule `operator_attested` in the manifest;
  * never an offer the lane would label differently: an offer of the retailer-role seller namespace
    (`agent_seed::retailer::<host>`, `ingestion.derive_merchant_id` for source_role='retailer') gets
    `retailer` from the lane, so it is REPORTED, never written;
  * never an offer whose own host is not the host the verdict judged (the listing's): REPORTED as a near miss;
  * never over an existing label: the write requires `offer_type IS NULL` (and `is_first_party` FALSE), and any
    drift since the plan aborts the whole transaction.

`updated_at` is NOT bumped: a label is not a new price observation, and `ORDER BY o.updated_at` readers must not
reorder over it. Every touched content_key's agent_pdp_view is rebuilt and its serving eligibility recomputed after
the commit (the view carries the offer's seller type), reusing scripts/restamp_offer_market.refresh_after.

  Dry run (default; writes nothing):
    python -m scripts.relabel_offer_seller_type
  Apply (manifest STORED in identity_resolution_events before the write; one transaction):
    python -m scripts.relabel_offer_seller_type --apply
  Record an APPROVED attestation list first (dry run unless --apply; never revives a revoked row):
    python -m scripts.relabel_offer_seller_type attest --file scripts/attestations/2026-10-06-public-brand-stores.json
  Revert a run (all or nothing; refused while a LATER applied run holds any of its offers), or re-run only its
  view / eligibility rebuild:
    python -m scripts.relabel_offer_seller_type revert --run-id relabel_<hex>
    python -m scripts.relabel_offer_seller_type refresh --run-id relabel_<hex> [--reverse]

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
from services.offer_seller_identity import (  # noqa: E402
    OFFER_TYPE_BRAND_DIRECT,
    derive_offer_seller_identity,
)
from scripts import restamp_offer_market as _restamp  # noqa: E402  (host normaliser + view rebuild)
from db import merchant_domain_attestations as _attest  # noqa: E402

LANE = "catalog_enrichment_agent_v1"
RETAILER_SELLER_PREFIX = "agent_seed::retailer::"

CANDIDATES_SQL = """
SELECT co.offer_id, co.merchant_id, co.source_domain AS offer_domain, cp.product_key, cp.content_key,
       cp.brand, cp.source_domain, cp.canonical_url
FROM catalog_offers co
JOIN catalog_products cp ON cp.product_key = co.product_key
WHERE co.source_system = :lane AND cp.source_system = :lane
  AND co.offer_type IS NULL
ORDER BY co.offer_id
"""

# Drift-guarded: an offer labelled (or relabelled) by anyone since the plan aborts the transaction.
LABEL_SQL = """
UPDATE catalog_offers SET offer_type = :offer_type, is_first_party = TRUE
WHERE offer_id = ANY(:ids) AND offer_type IS NULL AND is_first_party IS FALSE
RETURNING offer_id
"""
UNLABEL_SQL = """
UPDATE catalog_offers SET offer_type = NULL, is_first_party = FALSE
WHERE offer_id = ANY(:ids) AND offer_type = :offer_type AND is_first_party IS TRUE
RETURNING offer_id
"""

MANIFEST_ACTION = "offer_seller_type_relabel_manifest"
APPLIED_ACTION, REVERTED_ACTION = "offer_seller_type_relabel_applied", "offer_seller_type_relabel_reverted"
EVENT_SQL = _restamp.EVENT_SQL
RUN_STATE_SQL = _restamp.RUN_STATE_SQL
LOAD_MANIFEST_SQL = _restamp.LOAD_MANIFEST_SQL
LATER_LIVE_RUNS_SQL = _restamp.LATER_LIVE_RUNS_SQL
SAMPLE = _restamp.SAMPLE


def verdict_for(row: Mapping[str, Any], attested: Optional[Mapping[str, set]] = None) -> Dict[str, Any]:
    """The lane's own call, from the listing's own fields (ingestion._offer_seller_identity).

    When the rule finds no evidence and an operator attested the listing's own domain as the official
    store of this offer's seller (db/merchant_domain_attestations.py, migration 259), that domain is
    the rule's `official_domain` input -- the rule itself still decides (its official_domain_match
    branch), and its known-retailer branch still wins over any attestation."""
    v = derive_offer_seller_identity(
        domain=row.get("source_domain"), canonical_url=row.get("canonical_url"), brand=row.get("brand"),
    )
    if v["offer_type"] is None and attested:
        host = _attest.attestation_host(row.get("source_domain") or row.get("canonical_url"))
        if host and host in attested.get(str(row.get("merchant_id") or ""), set()):
            v = derive_offer_seller_identity(
                domain=row.get("source_domain"), canonical_url=row.get("canonical_url"),
                brand=row.get("brand"), official_domain=host,
            )
            if v["offer_type"] == OFFER_TYPE_BRAND_DIRECT:
                v = {**v, "rule": "operator_attested"}
    return v


async def plan(db: Any) -> Dict[str, Any]:
    rows = await db.fetch_all(CANDIDATES_SQL, {"lane": LANE})
    attested = await _attest.active_attested_domains({r["merchant_id"] for r in rows}, db=db)
    offers: List[Dict[str, Any]] = []
    rules: Counter = Counter()
    retailer_role: List[str] = []
    near_misses: List[Dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        v = verdict_for(row, attested)
        rules[f"{v['offer_type']}/{v['rule']}"] += 1
        if v["offer_type"] != OFFER_TYPE_BRAND_DIRECT or not v["is_first_party"]:
            continue
        if str(row.get("merchant_id") or "").startswith(RETAILER_SELLER_PREFIX):
            retailer_role.append(row["offer_id"])
            continue
        judged = _restamp.offer_host(v.get("evidence_domain"))
        own = _restamp.offer_host(row.get("offer_domain"))
        if not judged or own != judged:
            near_misses.append({"offer_id": row["offer_id"], "offer_host": own, "judged_host": judged})
            continue
        offers.append({"offer_id": row["offer_id"], "content_key": row.get("content_key"),
                       "product_key": row["product_key"], "rule": v["rule"], "host": judged})
    return {"offers": offers, "rules": dict(rules), "retailer_role": retailer_role, "near_misses": near_misses,
            "candidates": len(rows)}


def summary(p: Mapping[str, Any]) -> Dict[str, Any]:
    offers = p["offers"]
    return {
        "unlabelled_lane_offers": p["candidates"],
        "verdicts": p["rules"],
        "to_label_brand_direct": len(offers),
        "listings": len({o["product_key"] for o in offers}),
        "content_keys": len({o["content_key"] for o in offers if o.get("content_key")}),
        "hosts": len({o["host"] for o in offers}),
        "by_rule": dict(Counter(o["rule"] for o in offers)),
        "skipped_retailer_role": len(p["retailer_role"]),
        "skipped_near_misses": len(p["near_misses"]),
        "near_miss_sample": p["near_misses"][:SAMPLE],
        "sample": [o["offer_id"] for o in offers[:SAMPLE]],
    }


async def _record(db: Any, action: str, run_id: str, detail: Mapping[str, Any]) -> None:
    row = await db.fetch_one(EVENT_SQL, {"action": action, "run_id": run_id,
                                         "detail": json.dumps(detail, default=str)})
    if not row:
        raise RuntimeError(f"{action} for {run_id} was not recorded")


async def load_manifest(db: Any, run_id: str) -> Dict[str, Any]:
    row = await db.fetch_one(LOAD_MANIFEST_SQL, {"action": MANIFEST_ACTION, "run_id": run_id})
    detail = row and row["detail"]
    if isinstance(detail, str):
        detail = json.loads(detail)
    if not detail:
        raise SystemExit(f"no stored manifest for {run_id}")
    return detail


async def apply(db: Any, p: Mapping[str, Any]) -> Dict[str, Any]:
    if not p["offers"]:
        raise SystemExit("nothing to label")
    run_id = f"relabel_{uuid.uuid4().hex[:12]}"
    m = {"run_id": run_id, "offer_type": OFFER_TYPE_BRAND_DIRECT, "offers": p["offers"]}
    await _record(db, MANIFEST_ACTION, run_id, m)  # committed on its own, BEFORE the write
    ids = [o["offer_id"] for o in m["offers"]]
    async with db.transaction():
        got = await db.fetch_all(LABEL_SQL, {"ids": ids, "offer_type": m["offer_type"]})
        if len(got) != len(ids):
            raise RuntimeError(f"drift: {len(ids) - len(got)} of {len(ids)} offer(s) were labelled since the plan; "
                               "nothing written")
        await _record(db, APPLIED_ACTION, run_id, {"labelled": len(got)})
    print(f"COMMITTED {run_id}: {len(got)} offer(s) labelled {m['offer_type']}", flush=True)
    return {"run_id": run_id, "labelled": len(got),
            "refresh": await _restamp.refresh_after(db, m, source=run_id)}


async def revert(db: Any, run_id: str) -> Dict[str, Any]:
    m = await load_manifest(db, run_id)
    state = {r["action"]: r["id"] for r in await db.fetch_all(
        RUN_STATE_SQL, {"run_id": run_id, "actions": [APPLIED_ACTION, REVERTED_ACTION]})}
    if APPLIED_ACTION not in state:
        raise SystemExit(f"{run_id} never committed: nothing to revert")
    if REVERTED_ACTION in state:
        raise SystemExit(f"{run_id} is already reverted")
    mine = {o["offer_id"] for o in m["offers"]}
    for r in await db.fetch_all(LATER_LIVE_RUNS_SQL, {"applied": APPLIED_ACTION, "reverted": REVERTED_ACTION,
                                                      "after_id": state[APPLIED_ACTION]}):
        later = await load_manifest(db, r["run_id"])
        shared = mine & {o["offer_id"] for o in later["offers"]}
        if shared:
            raise SystemExit(f"refused: later run {r['run_id']} labelled {len(shared)} of these offers; revert it first")
    async with db.transaction():
        got = await db.fetch_all(UNLABEL_SQL, {"ids": sorted(mine), "offer_type": m["offer_type"]})
        done = {"unlabelled": len(got), "already": len(mine) - len(got)}
        await _record(db, REVERTED_ACTION, run_id, done)
    print(f"REVERTED {run_id}: {done['unlabelled']} offer(s), {done['already']} no longer carrying this label",
          flush=True)
    return {"run_id": run_id, **done, "refresh": await _restamp.refresh_after(db, m, source=f"{run_id}:revert")}


async def attest(db: Any, path: str, *, apply: bool) -> Dict[str, Any]:
    """Record an APPROVED list of operator attestations (JSON: a list of {merchant_id, domain,
    attested_by, review_ref, evidence: {sources: [...]}}). Dry run unless --apply. Existing active rows
    are left as they are; a revoked row is reported and never revived."""
    with open(path, encoding="utf-8") as fh:
        entries = _attest.validate_entries(json.load(fh))
    p = await _attest.plan_attestations(entries, db=db)
    out: Dict[str, Any] = {"entries": len(entries), "new": len(p["new"]), "already_active": len(p["already_active"]),
                           "revoked_not_revived": p["revoked"]}
    if apply and p["new"]:
        out["written"] = await _attest.write_attestations(p["new"], db=db)
    return out


async def run(args: argparse.Namespace) -> int:
    await database.connect()
    try:
        out: Dict[str, Any] = {}
        if args.command == "attest":
            res = await attest(database, args.file, apply=args.apply)
            print("ATTEST " + json.dumps(res, default=str), flush=True)
            if not args.apply:
                print("dry run: nothing written (--apply to write)", flush=True)
            return 0
        if args.command == "revert":
            out = await revert(database, args.run_id)
            print("REVERT_DONE " + json.dumps(out, default=str), flush=True)
        elif args.command == "refresh":
            m = await load_manifest(database, args.run_id)
            src = f"{args.run_id}:revert" if args.reverse else args.run_id
            out = {"refresh": await _restamp.refresh_after(database, m, source=src)}
            print("REFRESHED " + json.dumps(out, default=str), flush=True)
        else:
            p = await plan(database)
            print("PLAN " + json.dumps(summary(p), default=str), flush=True)
            if args.apply:
                out = await apply(database, p)
                print("APPLIED " + json.dumps(out, default=str), flush=True)
            else:
                print("dry run: nothing written (--apply to write)", flush=True)
        return _restamp.exit_code(out)
    finally:
        await database.disconnect()


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", nargs="?", default="plan", choices=["plan", "revert", "refresh", "attest"])
    p.add_argument("--file", help="attest: the approved attestation list (JSON)")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--run-id", dest="run_id")
    p.add_argument("--reverse", action="store_true", help="refresh: the run was reverted")
    args = p.parse_args(argv)
    if args.command in ("revert", "refresh") and not args.run_id:
        p.error(f"{args.command} needs --run-id")
    if args.command == "attest" and not args.file:
        p.error("attest needs --file")
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
