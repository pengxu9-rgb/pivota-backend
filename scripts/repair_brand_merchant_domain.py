#!/usr/bin/env python3
"""Restore a brand seller row's store domain after a multi-brand retailer run overwrote it.

WHY. `catalog_enrichment_agent` keys a brand store's seller row `agent_seed::<brand>`
(ingestion.derive_merchant_id) and upserts its `source_ref` / `metadata_json.domain` with the offer's host
(ingestion._build_merchant_upserts; apply._MERCHANT_UPSERT_SQL: last writer wins on source_ref, merge on
metadata). A run over a MULTI-brand retailer without a `seller_domain` keys every product by its brand instead,
so it overwrites those brands' rows with the retailer's host. Census 2026-10-07 (prod, read-only): one cocomo.sg
run, 2026-09-06 17:53Z, left 75 brand rows saying `cocomo.sg`; that run's offers are all suppressed and nothing
has rewritten the rows since. Nine of them still sell live from their own store -- AXIS-Y (axis-y.com), Round Lab,
Beauty of Joseon, SKIN1004, Anua, iUNIK, Centellian24, Isntree, REJURAN -- and the gateway's brand-direct
own-listing rule (PIVOTA-Agent src/services/canonicalOwnOfferSellerSql.js) requires that row's domain to be the
listing's own host, so their verified current money can never be read: 150 public labelled listings.

WHAT IT WRITES. Only what a re-run of the brand's own store would write, and only where the catalog shows it:
  * scope is an INCIDENT: rows whose current host is one of `--polluted-host` (required; e.g. cocomo.sg);
  * the target is `ingestion._domain_of(...)` -- the lane's own normaliser -- of each live lane offer's
    destination (`source_ref`) AND canonical URL (`offer_payload.canonical_url`; the lane keys the seller row on
    canonical-or-destination), and of every listing those offers sit on: all must name ONE host. A row with no
    live offer (65 of the 75 in prod), a listing or offer without a host (Medicube), offers from several hosts,
    a known-retailer target, or metadata that is not a JSON object is REPORTED and left alone;
  * `source_ref` and `metadata_json.domain` are set; every other metadata key is kept (merge), as the lane's
    upsert does. `updated_at` is not bumped. Nothing caches the domain (agent_pdp_view reads merchant_name only),
    so there is nothing to rebuild.
  * a row that changed since the plan aborts the whole transaction;
  * revert restores a row only while it still carries exactly what the repair wrote, including the
    `updated_at` the repair left (stored per row in the applied record): a row any writer touched since --
    even to the same host, e.g. a re-run of the brand's own store -- is left and reported, never re-polluted.

It does NOT label any offer and does NOT make a store official: Centellian24 / Isntree / REJURAN offers stay
unlabelled until a seller-type decision (scripts/relabel_offer_seller_type.py, attestations) says otherwise.

  Dry run (default; writes nothing):
    python -m scripts.repair_brand_merchant_domain --polluted-host cocomo.sg
  Apply (manifest STORED in identity_resolution_events before the write; one transaction):
    python -m scripts.repair_brand_merchant_domain --polluted-host cocomo.sg --apply
  Revert a run (all or nothing per row: a row changed since the repair is left and reported):
    python -m scripts.repair_brand_merchant_domain revert --run-id <run_id>
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from collections import Counter, defaultdict
from typing import Any, Dict, List, Mapping, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.database import database  # noqa: E402
from services.catalog_enrichment_agent.ingestion import _domain_of  # noqa: E402  (the lane's host normaliser)
from services.seller_identity import is_known_retailer_domain  # noqa: E402
from scripts import restamp_offer_market as _restamp  # noqa: E402  (event SQL)

LANE = "catalog_enrichment_agent_v1"
BRAND_SELLER_PREFIX = "agent_seed::"
RETAILER_SELLER_PREFIX = "agent_seed::retailer::"

ROWS_SQL = """
SELECT merchant_id, source_ref, metadata_json->>'domain' AS domain, CAST(updated_at AS text) AS updated_at,
       jsonb_typeof(metadata_json) AS metadata_type, status, indexable
FROM catalog_merchants
WHERE source_system = :lane
  AND starts_with(merchant_id, CAST(:brand_prefix AS text))
  AND NOT starts_with(merchant_id, CAST(:retailer_prefix AS text))
ORDER BY merchant_id
"""
# Every live lane offer under the row, with the listing it sits on.
EVIDENCE_SQL = """
SELECT co.merchant_id, co.offer_id, co.source_ref AS offer_url, co.offer_payload->>'canonical_url' AS offer_canonical_url,
       co.offer_type, cp.source_domain AS listing_domain
FROM catalog_offers co
JOIN catalog_products cp ON cp.product_key = co.product_key
WHERE co.merchant_id = ANY(:ids) AND co.source_system = :lane
  AND co.suppressed_at IS NULL AND co.suppression_reason IS NULL
"""
# Drift-guarded on the exact values the plan read.
SET_SQL = """
UPDATE catalog_merchants
SET source_ref = CAST(:to_host AS text),
    metadata_json = COALESCE(metadata_json, '{}'::jsonb) || jsonb_build_object('domain', CAST(:to_host AS text))
WHERE merchant_id = :merchant_id AND source_system = :lane
  AND source_ref IS NOT DISTINCT FROM CAST(:from_ref AS text)
  AND metadata_json->>'domain' IS NOT DISTINCT FROM CAST(:from_domain AS text)
  AND (metadata_json IS NULL OR jsonb_typeof(metadata_json) = 'object')
RETURNING merchant_id, CAST(updated_at AS text) AS updated_at
"""
UNSET_SQL = """
UPDATE catalog_merchants
SET source_ref = CAST(:from_ref AS text),
    metadata_json = CASE WHEN CAST(:from_metadata_null AS boolean) THEN NULL
                         WHEN CAST(:from_domain AS text) IS NULL THEN metadata_json - 'domain'
                         ELSE metadata_json || jsonb_build_object('domain', CAST(:from_domain AS text)) END
WHERE merchant_id = :merchant_id
  AND source_ref = CAST(:to_host AS text) AND metadata_json->>'domain' = CAST(:to_host AS text)
  -- the updated_at the repair's own write left (the applied record): a row any writer touched since, even to
  -- the same host (a re-run of the brand's own store bumps it), is left alone rather than re-polluted.
  AND CAST(updated_at AS text) IS NOT DISTINCT FROM CAST(:repaired_updated_at AS text)
RETURNING merchant_id
"""

MANIFEST_ACTION = "brand_merchant_domain_repair_manifest"
APPLIED_ACTION, REVERTED_ACTION = "brand_merchant_domain_repair_applied", "brand_merchant_domain_repair_reverted"
SAMPLE = _restamp.SAMPLE


def host_of(value: Optional[str]) -> Optional[str]:
    """A stored host or URL in the form the lane writes (lower case, no www.)."""
    raw = str(value or "").strip()
    if not raw:
        return None
    return _domain_of(raw if "://" in raw else f"https://{raw}")


def decide(row: Mapping[str, Any], evidence: List[Mapping[str, Any]], polluted: set) -> Dict[str, Any]:
    """The target host for one row, or why there is none."""
    current = host_of(row.get("source_ref")) or host_of(row.get("domain"))
    if current not in polluted and host_of(row.get("domain")) not in polluted:
        return {"skip": "not_polluted"}
    if row.get("metadata_type") not in (None, "object"):
        return {"skip": "metadata_not_an_object"}
    if not evidence:
        return {"skip": "no_live_offer"}
    # The lane keys the seller row on _domain_of(canonical_url or destination_url); co.source_ref is the
    # destination. Both must name the one host.
    offer_hosts = {host_of(e.get("offer_url")) for e in evidence} | {
        host_of(e.get("offer_canonical_url")) for e in evidence if e.get("offer_canonical_url")}
    listing_hosts = {host_of(e.get("listing_domain")) for e in evidence}
    if None in offer_hosts or None in listing_hosts:
        return {"skip": "host_missing"}
    if len(offer_hosts) != 1:
        return {"skip": "offers_from_several_hosts", "hosts": sorted(offer_hosts)}
    target = next(iter(offer_hosts))
    if listing_hosts != {target}:
        return {"skip": "listing_host_disagrees", "hosts": sorted(listing_hosts | offer_hosts)}
    if target in polluted:
        return {"skip": "only_the_polluted_host"}
    if any(e.get("offer_type") == "retailer" for e in evidence) or is_known_retailer_domain(target):
        return {"skip": "target_is_a_retailer", "hosts": [target]}
    return {"to_host": target, "offers": len(evidence)}


async def plan(db: Any, polluted_hosts: List[str]) -> Dict[str, Any]:
    polluted = {h for h in (host_of(x) for x in polluted_hosts) if h}
    if not polluted:
        raise SystemExit("--polluted-host is required")
    rows = [dict(r) for r in await db.fetch_all(ROWS_SQL, {"lane": LANE, "brand_prefix": BRAND_SELLER_PREFIX,
                                                           "retailer_prefix": RETAILER_SELLER_PREFIX})]
    rows = [r for r in rows if host_of(r.get("source_ref")) in polluted or host_of(r.get("domain")) in polluted]
    evidence: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    if rows:
        for e in await db.fetch_all(EVIDENCE_SQL, {"ids": [r["merchant_id"] for r in rows], "lane": LANE}):
            evidence[e["merchant_id"]].append(dict(e))
    repairs: List[Dict[str, Any]] = []
    skipped: Counter = Counter()
    skipped_sample: List[Dict[str, Any]] = []
    for r in rows:
        d = decide(r, evidence.get(r["merchant_id"], []), polluted)
        if "to_host" in d:
            repairs.append({"merchant_id": r["merchant_id"], "from_ref": r.get("source_ref"),
                            "from_domain": r.get("domain"), "from_metadata_null": r.get("metadata_type") is None,
                            "from_updated_at": r.get("updated_at"), "to_host": d["to_host"],
                            "live_offers": d["offers"], "status": r.get("status"), "indexable": r.get("indexable")})
        else:
            skipped[d["skip"]] += 1
            if d["skip"] != "no_live_offer" and len(skipped_sample) < SAMPLE:
                skipped_sample.append({"merchant_id": r["merchant_id"], **d})
    return {"polluted_hosts": sorted(polluted), "rows_on_polluted_host": len(rows), "repairs": repairs,
            "skipped": dict(skipped), "skipped_sample": skipped_sample}


def summary(p: Mapping[str, Any]) -> Dict[str, Any]:
    return {"polluted_hosts": p["polluted_hosts"], "rows_on_polluted_host": p["rows_on_polluted_host"],
            "to_repair": len(p["repairs"]), "skipped": p["skipped"], "skipped_sample": p["skipped_sample"],
            "repairs": [(r["merchant_id"], r["from_ref"], r["to_host"], r["live_offers"], r["status"], r["indexable"])
                        for r in p["repairs"]]}


async def _record(db: Any, action: str, run_id: str, detail: Mapping[str, Any]) -> None:
    row = await db.fetch_one(_restamp.EVENT_SQL, {"action": action, "run_id": run_id,
                                                  "detail": json.dumps(detail, default=str)})
    if not row:
        raise RuntimeError(f"{action} for {run_id} was not recorded")


async def load_manifest(db: Any, run_id: str) -> Dict[str, Any]:
    row = await db.fetch_one(_restamp.LOAD_MANIFEST_SQL, {"action": MANIFEST_ACTION, "run_id": run_id})
    detail = row and row["detail"]
    if isinstance(detail, str):
        detail = json.loads(detail)
    if not detail:
        raise SystemExit(f"no stored manifest for {run_id}")
    return detail


async def apply(db: Any, p: Mapping[str, Any]) -> Dict[str, Any]:
    if not p["repairs"]:
        raise SystemExit("nothing to repair")
    run_id = f"merchdomain_{uuid.uuid4().hex[:12]}"
    m = {"run_id": run_id, "polluted_hosts": p["polluted_hosts"], "repairs": p["repairs"]}
    await _record(db, MANIFEST_ACTION, run_id, m)  # committed on its own, BEFORE the write
    async with db.transaction():
        left: Dict[str, Optional[str]] = {}
        for r in m["repairs"]:
            got = await db.fetch_one(SET_SQL, {"lane": LANE, **{k: r[k] for k in
                                               ("merchant_id", "from_ref", "from_domain", "to_host")}})
            if not got:
                raise RuntimeError(f"drift: {r['merchant_id']} changed since the plan; nothing written")
            left[r["merchant_id"]] = got["updated_at"]
        done = len(left)
        await _record(db, APPLIED_ACTION, run_id, {"repaired": done, "updated_at": left})
    print(f"COMMITTED {run_id}: {done} seller row(s) restored to their own store host", flush=True)
    return {"run_id": run_id, "repaired": done}


async def revert(db: Any, run_id: str) -> Dict[str, Any]:
    m = await load_manifest(db, run_id)
    state = {r["action"] for r in await db.fetch_all(
        _restamp.RUN_STATE_SQL, {"run_id": run_id, "actions": [APPLIED_ACTION, REVERTED_ACTION]})}
    applied = await db.fetch_one(_restamp.LOAD_MANIFEST_SQL, {"action": APPLIED_ACTION, "run_id": run_id})
    applied_detail = (applied and applied["detail"]) or {}
    if isinstance(applied_detail, str):
        applied_detail = json.loads(applied_detail)
    left = applied_detail.get("updated_at") or {}
    if APPLIED_ACTION not in state:
        raise SystemExit(f"{run_id} never committed: nothing to revert")
    if REVERTED_ACTION in state:
        raise SystemExit(f"{run_id} is already reverted")
    reverted, changed = 0, []
    async with db.transaction():
        for r in m["repairs"]:
            got = await db.fetch_one(UNSET_SQL, {
                **{k: r.get(k) for k in ("merchant_id", "from_ref", "from_domain", "to_host")},
                "from_metadata_null": bool(r.get("from_metadata_null")),
                "repaired_updated_at": left.get(r["merchant_id"])})
            if got:
                reverted += 1
            else:
                changed.append(r["merchant_id"])
        await _record(db, REVERTED_ACTION, run_id, {"reverted": reverted, "changed_since": changed})
    print(f"REVERTED {run_id}: {reverted} row(s); {len(changed)} changed since the repair and left as they are",
          flush=True)
    return {"run_id": run_id, "reverted": reverted, "changed_since": changed}


async def run(args: argparse.Namespace) -> int:
    await database.connect()
    try:
        if args.command == "revert":
            print("REVERT_DONE " + json.dumps(await revert(database, args.run_id), default=str), flush=True)
            return 0
        p = await plan(database, args.polluted_host or [])
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
    p.add_argument("--polluted-host", action="append", help="the retailer host that overwrote brand rows (repeatable)")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--run-id", dest="run_id")
    args = p.parse_args(argv)
    if args.command == "revert" and not args.run_id:
        p.error("revert needs --run-id")
    if args.command == "plan" and not args.polluted_host:
        p.error("plan needs --polluted-host")
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
