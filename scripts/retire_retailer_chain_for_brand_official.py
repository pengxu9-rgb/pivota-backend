#!/usr/bin/env python3
"""Retire a brand's OWN storefront's retailer-mode chain once it is re-run as brand_official.

REVERSIBLE tombstone, never a delete -- the precedent of scripts/retire_superseded_brand_keys.py:
`catalog_products.suppression_reason` + `suppressed_at` + `suppression_metadata` carrying the run
id, `external_product_seeds.status='inactive'`, and the offer cascade. `revert` undoes a whole run
from its manifest, offers included.

WHY THIS EXISTS. us.mcobeauty.com and us.inikaorganic.com are the brands' own US stores. They were
crawled in retailer mode and passed `retailer_maker_unproven` only because their first host label,
"us", sat under the 3-char floor (#2362 closed that). So their rows were written as a RETAILER's
listings: `ext:retailer:` product keys, offers under `agent_seed::retailer::<host>`, reseller INCI
authority. The brand_official re-run writes CANONICAL keys and `apply_ingest_plan` upserts on
product_key, so it has no way to retire the retailer chain. `make_content_key` hashes only (brand, title,
GTIN), so both rows of a product share one cluster: without this step the store is listed as TWO sellers
of its own product, one of them labelled a retailer, and the seller count is inflated by one.

THE COHORT IS WHAT THE HOST WROTE AS A RETAILER, proven from both sides, or nothing is written:
  * the `ext:retailer:` rows whose source_domain is the host, and
  * the product keys carrying an offer from the host's retailer seller id,
must be the same set; every offer on those keys must be that seller's; every row must carry the
brand; the brand must own the host (`brand_owns_domain`, the same label-equality rule the
brand_official lane admits a store by). An `ext:retailer:` key is one listing on one host, so any
other shape is one this script was not written for.

ORDER. Run this AFTER the brand_official apply for the host has landed (its job is `done`), the
reverse of retire_superseded_brand_keys (Peng 2026-09-26). Retiring first leaves the store with no live
offers from the moment of retirement until the apply lands -- the drain queue, a held-flag approval and
a 20-40 min apply -- whereas the overlap the other order allows is one duplicate seller inside a cluster
that already exists. `--apply` therefore refuses unless a brand_official retailer_ingest job for the
host is `done`: the chain is only superseded once something has superseded it. The post-commit refresh
then rebuilds each cluster's view row with the brand-direct offer alone.

DRY-RUN BY DEFAULT. Nothing is written without --apply. Like every script that refreshes
agent_pdp_view, the apply's post-commit refresh can issue DDL (see build_agent_pdp_view_row).

  # 1. plan
  python scripts/retire_retailer_chain_for_brand_official.py --domain us.mcobeauty.com --brand MCoBeauty

  # 2. apply (one transaction; the manifest is printed to stdout, i.e. Cloud Logging, as well)
  python scripts/retire_retailer_chain_for_brand_official.py --domain us.mcobeauty.com --brand MCoBeauty \
      --apply --manifest /tmp/retire_mcobeauty.json

  # 3. undo the whole run (from the manifest file, or from the logged manifest line)
  python scripts/retire_retailer_chain_for_brand_official.py revert --manifest /tmp/retire_mcobeauty.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db.database import database  # noqa: E402
from scripts.retire_superseded_brand_keys import (  # noqa: E402
    DEACTIVATE_SEEDS_SQL, REACTIVATE_SEED_SQL, SEEDS_FOR_KEYS_SQL, UNSUPPRESS_SQL, _as_json_text,
)
from services.catalog_enrichment_agent.ingestion import _domain_of, derive_merchant_id  # noqa: E402
from services.catalog_offer_suppression import (  # noqa: E402
    CASCADE_LANE, CASCADE_LANE_KEY, PRODUCT_SUPPRESSED_REASON,
    cascade_for_suppressed_product_keys, revert_offer_suppression,
)
from services.curated_brand_feed import _brand_key, _clean_domain  # noqa: E402
from services.offer_seller_identity import brand_owns_domain  # noqa: E402

REASON = "retailer_chain_superseded_by_brand_official"
RETAILER_KEY_PREFIX = "ext:retailer:"
BRAND_OFFICIAL_JOB_STATUSES = ("queued", "apply_due", "held", "done")  # reported by the plan
SUPERSEDED_STATUS = "done"  # required by --apply: see ORDER

HOST_RETAILER_ROWS_SQL = """
SELECT product_key, content_key, merchant_id, brand, title, source_domain,
       suppression_reason, suppressed_at, suppression_metadata
FROM catalog_products
WHERE source_domain = :host
  AND product_key LIKE CAST(:key_pattern AS text)
"""

SELLER_OFFER_KEYS_SQL = """
SELECT DISTINCT product_key FROM catalog_offers WHERE merchant_id = :seller
"""

OFFERS_ON_KEYS_SQL = """
SELECT offer_id, product_key, merchant_id, suppressed_at, suppression_reason, suppression_metadata
FROM catalog_offers
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

LIVE_ROWS_SQL = """
SELECT product_key, suppression_reason FROM catalog_products WHERE product_key = ANY(:keys)
"""

BRAND_OFFICIAL_JOBS_SQL = """
SELECT id, status FROM retailer_ingest_jobs
WHERE domain = :host
  AND options->>'source_role' = 'brand_official'
  AND status = ANY(:statuses)
"""


def _meta(value: Any) -> Dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def retailer_seller_id(host: str) -> str:
    """The seller id the writer stamped on this host's retailer offers: `_build_offer_rows` passes the
    offer URL's `_domain_of` (www. stripped) as `domain` and the record's host as `seller_domain`."""
    return derive_merchant_id(None, _domain_of(f"https://{host}/"), seller_domain=host)


def check_cohort(
    host: str,
    brand: str,
    rows: Iterable[Dict[str, Any]],
    seller_offer_keys: Iterable[str],
    offers_on_keys: Iterable[Dict[str, Any]],
) -> Dict[str, Any]:
    """Pure. The live keys to retire, and every reason the cohort is not the shape this script
    retires (`problems`; non-empty refuses the apply). No DB, so the refusals are testable."""
    seller = retailer_seller_id(host)
    rows = [dict(r) for r in rows]
    row_keys = {str(r["product_key"]) for r in rows}
    offer_keys = {str(k) for k in seller_offer_keys}
    offers = [dict(o) for o in offers_on_keys]
    problems: List[str] = []
    if not brand_owns_domain(brand, host):
        problems.append(f"brand_does_not_own_host: {brand!r} is not {host}'s own brand")
    if not rows:
        problems.append(f"no_retailer_rows: no {RETAILER_KEY_PREFIX} rows with source_domain {host}")
    if row_keys != offer_keys:
        problems.append(
            f"cohort_sides_disagree: {len(row_keys - offer_keys)} row(s) without a {seller} offer "
            f"{sorted(row_keys - offer_keys)[:5]}, {len(offer_keys - row_keys)} {seller} offer key(s) "
            f"outside the rows {sorted(offer_keys - row_keys)[:5]}"
        )
    foreign = sorted({str(o["merchant_id"]) for o in offers if str(o.get("merchant_id")) != seller})
    if foreign:
        problems.append(f"foreign_offers: offers from {foreign[:5]} sit on the cohort's keys")
    other_brand = sorted({str(r.get("brand")) for r in rows if _brand_key(r.get("brand")) != _brand_key(brand)})
    if other_brand:
        problems.append(f"other_brand_rows: {other_brand[:5]} on {host}, not {brand!r}")
    # `revert` restores offers through revert_offer_suppression, which is keyed by product key. An
    # offer that ALREADY carries that lane's stamp would be restored by a revert of a run that never
    # suppressed it, so the run is refused rather than made silently over-revertible.
    prestamped = sorted(
        str(o["offer_id"]) for o in offers
        if o.get("suppressed_at") is not None and o.get("suppression_reason") == PRODUCT_SUPPRESSED_REASON
        and _meta(o.get("suppression_metadata")).get(CASCADE_LANE_KEY) == CASCADE_LANE
    )
    if prestamped:
        problems.append(f"offers_already_cascade_suppressed: {prestamped[:5]} ({len(prestamped)})")
    live = sorted(str(r["product_key"]) for r in rows if not r.get("suppression_reason"))
    return {
        "host": host, "brand": brand, "seller": seller, "problems": problems,
        "rows": {str(r["product_key"]): r for r in rows}, "live": live,
        "content_keys": sorted({str(r["content_key"]) for r in rows if r.get("content_key")}),
        "offers_live": sorted(str(o["offer_id"]) for o in offers if o.get("suppressed_at") is None),
        "offers_total": len(offers),
    }


async def plan(host: str, brand: str) -> Dict[str, Any]:
    rows = [dict(r) for r in await database.fetch_all(
        HOST_RETAILER_ROWS_SQL, {"host": host, "key_pattern": RETAILER_KEY_PREFIX + "%"})]
    seller = retailer_seller_id(host)
    offer_keys = [r["product_key"] for r in await database.fetch_all(SELLER_OFFER_KEYS_SQL, {"seller": seller})]
    keys = sorted({str(r["product_key"]) for r in rows} | {str(k) for k in offer_keys})
    offers = [dict(r) for r in await database.fetch_all(OFFERS_ON_KEYS_SQL, {"keys": keys})] if keys else []
    p = check_cohort(host, brand, rows, offer_keys, offers)
    p["seeds"] = [dict(r) for r in await database.fetch_all(SEEDS_FOR_KEYS_SQL, {"keys": p["live"]})] if p["live"] else []
    p["active_seeds"] = [s for s in p["seeds"] if str(s.get("status") or "").lower() == "active"]
    p["brand_official_jobs"] = [dict(r) for r in await database.fetch_all(
        BRAND_OFFICIAL_JOBS_SQL, {"host": host, "statuses": list(BRAND_OFFICIAL_JOB_STATUSES)})]
    return p


def print_plan(p: Dict[str, Any]) -> None:
    print(f"host              : {p['host']}  (brand {p['brand']!r}, seller {p['seller']})")
    print(f"retailer rows     : {len(p['rows'])}  (LIVE, would be tombstoned: {len(p['live'])})")
    print(f"offers on them    : {p['offers_total']}  (live, would cascade: {len(p['offers_live'])})")
    print(f"content keys      : {len(p['content_keys'])}  (view refresh + serving recompute after commit)")
    print(f"seeds attached    : {len(p['seeds'])}  (active, would deactivate: {len(p['active_seeds'])})")
    print(f"brand_official jobs for the host: {[(j['id'], j['status']) for j in p['brand_official_jobs']]}")
    for problem in p["problems"]:
        print(f"REFUSE: {problem}")
    for key in p["live"][:10]:
        print(f"  retire : {key}  {str(p['rows'][key].get('title'))[:60]}")
    print("PLAN_JSON " + json.dumps({
        "host": p["host"], "rows": len(p["rows"]), "live": len(p["live"]), "offers_total": p["offers_total"],
        "offers_live": len(p["offers_live"]), "content_keys": len(p["content_keys"]),
        "seeds": len(p["seeds"]), "active_seeds": len(p["active_seeds"]),
        "brand_official_jobs": p["brand_official_jobs"], "problems": p["problems"],
    }, default=str))


async def _refresh_and_recompute(content_keys: List[str]) -> Dict[str, int]:
    """After commit, best-effort, as scripts/merge_duplicate_canonicals.py does: rebuild each view row
    (a cluster that still has another seller keeps serving it) and recompute serving eligibility (a
    cluster left with no live row stops serving -- the refresh alone would leave the old view row)."""
    from services.agent_pdp_view_assembler import refresh_agent_pdp_view_for_content_key
    from services.index_pipeline_state_service import recompute_serving_eligibility

    counts = {"view_refreshed": 0, "view_unbuilt": 0, "view_failed": 0, "recomputed": 0, "recompute_failed": 0}
    for ck in content_keys:
        try:
            built = await refresh_agent_pdp_view_for_content_key(ck, refresh_source=REASON)
            counts["view_refreshed" if built else "view_unbuilt"] += 1
        except Exception as exc:  # noqa: BLE001 -- the tombstone is committed; report, do not undo
            counts["view_failed"] += 1
            print(f"    WARN view refresh failed for {ck}: {str(exc)[:120]}")
        try:
            await recompute_serving_eligibility(ck, reason=REASON)
            counts["recomputed"] += 1
        except Exception as exc:  # noqa: BLE001
            counts["recompute_failed"] += 1
            print(f"    WARN serving recompute failed for {ck}: {str(exc)[:120]}")
    return counts


async def apply(p: Dict[str, Any], manifest_path: str) -> Dict[str, Any]:
    if p["problems"]:
        raise SystemExit(f"refused, nothing written: {p['problems']}")
    done = [j for j in p["brand_official_jobs"] if j.get("status") == SUPERSEDED_STATUS]
    if not done:
        raise SystemExit(f"refused, nothing written: no {SUPERSEDED_STATUS} brand_official job for {p['host']} has "
                         f"superseded this chain yet ({[(j['id'], j['status']) for j in p['brand_official_jobs']]})")
    keys = p["live"]
    if not keys:
        print("nothing live to retire -- no write.")
        return {}
    run_id = f"retire_{uuid.uuid4().hex[:12]}"
    at = datetime.now(timezone.utc).isoformat()
    metadata = json.dumps({
        "run_id": run_id, "reason": REASON, "pr": "pivota-backend#2362", "host": p["host"], "brand": p["brand"],
        "superseded_by_jobs": [j["id"] for j in done],
        "note": "brand's own store written as a retailer listing chain; re-run as brand_official", "at": at,
    })
    # BEFORE-state first: a manifest written after the write cannot describe what it replaced.
    manifest = {
        "run_id": run_id, "reason": REASON, "host": p["host"], "brand": p["brand"], "at": at,
        "products": [
            {
                "product_key": k,
                "prior_suppression_reason": p["rows"][k].get("suppression_reason"),
                "prior_suppressed_at": (
                    p["rows"][k]["suppressed_at"].isoformat() if p["rows"][k].get("suppressed_at") else None
                ),
                "prior_suppression_metadata": p["rows"][k].get("suppression_metadata"),
            } for k in keys
        ],
        "seeds": [{"id": str(s["id"]), "prior_status": s.get("status")} for s in p["active_seeds"]],
        "content_keys": p["content_keys"],
    }
    Path(manifest_path).write_text(json.dumps(manifest, indent=1, default=str))
    print(f"manifest written BEFORE the write: {manifest_path}")
    # AND to stdout: a Cloud Run Job's filesystem dies with the container, so the logged copy is the
    # one that makes the run revertible (see retire_superseded_brand_keys.py).
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
    counts: Dict[str, Any] = {"products": len(keys), "seeds": len(seed_rows), "offers": len(offer_ids)}
    counts.update(await _refresh_and_recompute(p["content_keys"]))
    print("APPLIED_JSON " + json.dumps({"run_id": run_id, **counts}))
    return counts


async def revert(manifest_path: str) -> None:
    m = json.loads(Path(manifest_path).read_text())
    keys = [row["product_key"] for row in m["products"]]
    async with database.transaction():
        for row in m["products"]:
            await database.execute(UNSUPPRESS_SQL, {
                "key": row["product_key"], "reason": row["prior_suppression_reason"],
                "suppressed_at": row["prior_suppressed_at"],
                "metadata": _as_json_text(row["prior_suppression_metadata"]),
            })
        for s in m.get("seeds") or []:
            await database.execute(REACTIVATE_SEED_SQL, {"id": s["id"], "status": s["prior_status"]})
        # Exact: the apply refused a cohort with an offer already carrying this lane's stamp.
        offer_ids = await revert_offer_suppression(keys)
    counts = await _refresh_and_recompute(m.get("content_keys") or [])
    print(f"reverted run {m['run_id']}: {len(keys)} product(s), {len(m.get('seeds') or [])} seed(s), "
          f"{len(offer_ids)} offer(s); {counts}")


async def run(args: argparse.Namespace) -> int:
    await database.connect()
    try:
        if args.command == "revert":
            await revert(args.manifest)
            return 0
        p = await plan(_clean_domain(args.domain), args.brand)
        print_plan(p)
        if not args.apply:
            print("\nDRY-RUN -- re-run with --apply (and --manifest) to tombstone.")
            return 1 if p["problems"] else 0
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
