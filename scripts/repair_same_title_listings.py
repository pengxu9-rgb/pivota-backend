#!/usr/bin/env python3
"""Repair content-keyed rows that carry more than one listing of one store: one listing per row per host.

WHY. derive_product_key keys a brand_official row by (brand, title) alone, so every same-title listing of a store
landed its offers and seeds under ONE row (fixed going forward by ingestion.ingest_validated_jsonl, #2463). Census
2026-09-29: 189 live rows carry more than one page of one host; in 109 the row's canonical_url names a page other
than its live offer's (the upsert re-points canonical_url/image_url, while scripts/reconcile_catalog_offers keeps
whichever offer on the shared `::canonical` SKU was touched last); 47 sell two or more pages at once; 114 keep active
seeds on several pages, which the seed refresh re-touches.

WHAT IT DOES, per live row and host with more than one listing, for ONE market (--market, default US: the keeper is
elected for that market's buyer; other markets' offers and seeds are counted in `other_market_left`, never written). The keeper is ingestion.elect_listing_keeper -- the
SAME function the ingest uses, fed from the catalog (see `evidence`), with the row's current listing as the
tie-break -- so the next ingest keeps the page this repair keeps instead of bringing a second one back:
  * suppress every LIVE offer of the other listings (reason `same_key_other_listing`, the run and keeper in
    suppression_metadata);
  * restore the keeper's offers the reconciler suppressed as `duplicate_offer` -- only where no other live offer
    would sit on the same (sku_key, channel, market) shelf, and only the one whose PRICE was read most recently
    (price_checked_at; the dry run counts restored offers by that date -- "never" is a price of unknown age, which
    stays what it was until the seed refresh re-reads the keeper's page: apply just before a refresh run);
  * deactivate every ACTIVE seed of the other listings (status 'inactive'), so the seed refresh stops re-touching
    their offers;
  * re-point the row's canonical_url / image_url to the keeper's page when they name another listing of that host
    (the title is the same by construction; a keeper with no known URL or image skips the row/host whole).
A row/host is SKIPPED whole and reported when its keeper would be left with no live offer (the repair never takes the
last live offer away from a row), or when even its keeper is gone or sold out (every listing unavailable: nothing to
align a row to -- services/external_seed_destination_liveness owns dead pages).

Nothing is deleted. The manifest (every prior value) is STORED in identity_resolution_events before the write; the
write is one transaction, and each statement is guarded on the exact value read at plan time -- any drift aborts it
all. `updated_at` is not bumped (the reconciler's keeper election and ORDER BY updated_at readers must not reorder
over a repair); every touched content_key's served view and eligibility are rebuilt after the commit.

  Dry run (default; writes nothing):
    python -m scripts.repair_same_title_listings [--host cocodor.com ...] [--product-key ext:... ...] [--market US]
  Apply (Peng's go; AFTER the drain is re-imaged onto #2463 -- the old drain image re-points rows and re-activates
  the left-out pages' seeds on its next run of a store, undoing this; the new one never moves a row off its listing):
    ... --apply
  Revert a run (all or nothing; refused while a LATER applied run holds any of its rows), or re-run its rebuild:
    python -m scripts.repair_same_title_listings revert --run-id samelisting_<hex>
    python -m scripts.repair_same_title_listings refresh --run-id samelisting_<hex>

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
from typing import Any, Dict, List, Mapping, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.database import database  # noqa: E402
from services.catalog_enrichment_agent.ingestion import content_listing, elect_listing_keeper  # noqa: E402
from services.external_seed_destination_liveness import CONFIRMED_DEAD_VERDICTS  # noqa: E402

SOURCE_SYSTEM = "catalog_enrichment_agent_v1"
REASON = "same_key_other_listing"
DUPLICATE_REASON = "duplicate_offer"  # scripts/reconcile_catalog_offers.REASON_DUPLICATE

ROWS_SQL = """
SELECT product_key, content_key, canonical_url, image_url
FROM catalog_products
WHERE source_system = :source_system AND suppressed_at IS NULL
  AND product_key LIKE 'ext:%' AND product_key NOT LIKE 'ext:retailer:%'
  AND (CAST(:keys AS text[]) IS NULL OR product_key = ANY(CAST(:keys AS text[])))
ORDER BY product_key
"""
OFFERS_SQL = """
SELECT o.offer_id, o.product_key, o.sku_key, o.channel, o.market, o.availability, o.updated_at, o.price_checked_at,
       o.suppressed_at, o.suppression_reason, o.suppression_metadata, o.source_ref,
       o.offer_payload->>'destination_url' AS destination_url, o.offer_payload->>'canonical_url' AS canonical_url,
       o.offer_payload->>'image_url' AS image_url, s.source_variant_id
FROM catalog_offers o LEFT JOIN catalog_skus s ON s.sku_key = o.sku_key
WHERE o.product_key = ANY(:keys)
ORDER BY o.offer_id
"""
SEEDS_SQL = """
SELECT id, attached_product_key AS product_key, canonical_url, destination_url, image_url, status, availability,
       destination_verdict, market, updated_at
FROM external_product_seeds WHERE attached_product_key = ANY(:keys)
ORDER BY id
"""

SUPPRESS_SQL = """
UPDATE catalog_offers
SET suppressed_at = NOW(), suppression_reason = :reason,
    suppression_metadata = coalesce(suppression_metadata, CAST('{}' AS jsonb)) || CAST(:metadata AS jsonb)
WHERE offer_id = ANY(:ids) AND suppressed_at IS NULL
RETURNING offer_id
"""
RESTORE_SQL = """
UPDATE catalog_offers SET suppressed_at = NULL, suppression_reason = NULL
WHERE offer_id = ANY(:ids) AND suppressed_at IS NOT NULL AND suppression_reason = :reason
RETURNING offer_id
"""
# Another live offer on a restored offer's shelf, written since the plan: the restore would duplicate it.
SHELF_TAKEN_SQL = """
SELECT o.offer_id, s.offer_id AS live_offer_id
FROM catalog_offers o
JOIN catalog_offers s ON s.sku_key = o.sku_key AND s.channel = o.channel
     AND coalesce(s.market, '') = coalesce(o.market, '') AND s.offer_id <> o.offer_id
WHERE o.offer_id = ANY(:ids) AND s.suppressed_at IS NULL
"""
SEED_STATUS_SQL = """
UPDATE external_product_seeds SET status = :status, updated_at = NOW()
WHERE id = ANY(:ids) AND status = :prior
RETURNING id
"""
# Revert: one seed back to its status AND its updated_at as read at plan time (refresh lanes order on it).
SEED_RESTORE_SQL = """
UPDATE external_product_seeds
SET status = :status, updated_at = CAST(CAST(:prior_updated_at AS text) AS timestamptz)
WHERE id = :id AND status = :prior
RETURNING id
"""
REPOINT_SQL = """
UPDATE catalog_products SET canonical_url = :canonical_url, image_url = :image_url
WHERE product_key = :product_key
  AND canonical_url IS NOT DISTINCT FROM :prior_canonical_url AND image_url IS NOT DISTINCT FROM :prior_image_url
RETURNING product_key
"""
# Revert: an offer this run suppressed goes back live only while it still carries this run's suppression.
UNSUPPRESS_SQL = """
UPDATE catalog_offers
SET suppressed_at = NULL, suppression_reason = NULL,
    suppression_metadata = CAST(CAST(:prior_metadata AS text) AS jsonb)
WHERE offer_id = :offer_id AND suppression_reason = :reason
  AND suppression_metadata->>'same_listing_run' = :run_id
RETURNING offer_id
"""
RESUPPRESS_SQL = """
UPDATE catalog_offers
SET suppressed_at = CAST(CAST(:prior_suppressed_at AS text) AS timestamptz), suppression_reason = :reason
WHERE offer_id = :offer_id AND suppressed_at IS NULL
RETURNING offer_id
"""

MANIFEST_ACTION = "same_title_listing_repair_manifest"
APPLIED_ACTION, REVERTED_ACTION = "same_title_listing_repair_applied", "same_title_listing_repair_reverted"
EVENT_SQL = """
INSERT INTO identity_resolution_events (proposal_id, action, run_id, detail)
VALUES (NULL, :action, :run_id, CAST(:detail AS jsonb))
RETURNING id
"""
RUN_STATE_SQL = """
SELECT action, id FROM identity_resolution_events WHERE run_id = :run_id AND action = ANY(:actions)
"""
LOAD_MANIFEST_SQL = """
SELECT detail FROM identity_resolution_events WHERE action = :action AND run_id = :run_id
ORDER BY id DESC LIMIT 1
"""
LATER_LIVE_RUNS_SQL = """
SELECT a.run_id FROM identity_resolution_events a
WHERE a.action = :applied AND a.id > :after_id
  AND NOT EXISTS (SELECT 1 FROM identity_resolution_events r WHERE r.run_id = a.run_id AND r.action = :reverted)
"""

SAMPLE = 20


def _json(value: Any) -> Dict[str, Any]:
    if isinstance(value, str):
        return json.loads(value or "{}")
    return dict(value or {})


def evidence(handle: str, offers: List[Mapping[str, Any]], seeds: List[Mapping[str, Any]]) -> Dict[str, Any]:
    """elect_listing_keeper's inputs for one listing, from the catalog rather than a crawl.

    available: False when the page is gone (every seed of it has a CONFIRMED_DEAD verdict), or when nothing
    sells it in stock (no live offer 'in_stock', no active seed 'in_stock') while something says out of stock;
    None when nothing is known. oldest_variant_id: the lowest numeric storefront variant id among its offers' SKUs
    (most rows keep only the `::canonical` SKU, so usually None -- the row's current listing decides a tie)."""
    verdicts = [str(s.get("destination_verdict") or "") for s in seeds]
    dead = bool(verdicts) and all(v in CONFIRMED_DEAD_VERDICTS for v in verdicts)
    stock = [str(o.get("availability") or "") for o in offers if o.get("suppressed_at") is None]
    stock += [str(s.get("availability") or "") for s in seeds if s.get("status") == "active"]
    if dead:
        available: Optional[bool] = False
    elif "in_stock" in stock:
        available = True
    elif "out_of_stock" in stock:
        available = False
    else:
        available = None
    ids = [int(str(o["source_variant_id"])) for o in offers if str(o.get("source_variant_id") or "").isdigit()]
    return {"handle": handle, "available": available, "oldest_variant_id": min(ids) if ids else None}


def _listing_of_offer(o: Mapping[str, Any]) -> Optional[Tuple[str, str]]:
    return content_listing(o.get("destination_url") or o.get("canonical_url") or o.get("source_ref"))


def _listing_of_seed(s: Mapping[str, Any]) -> Optional[Tuple[str, str]]:
    return content_listing(s.get("canonical_url") or s.get("destination_url"))


def _in_market(item: Mapping[str, Any], market: str) -> bool:
    """An offer or seed of `market` (a row with no market is the catalog's default, US)."""
    return str(item.get("market") or "US").strip().upper() == market.strip().upper()


def plan_row(row: Mapping[str, Any], offers: List[Mapping[str, Any]], seeds: List[Mapping[str, Any]], *,
             market: str) -> List[Dict[str, Any]]:
    """One entry per host of this row that carries more than one listing: the keeper and every change.

    Only `market`'s offers and seeds are changed (the keeper is elected for that market; a GB job on the same
    host would elect its own): other markets' are counted in `other_market_left`, never written."""
    by_host: Dict[str, Dict[str, Dict[str, list]]] = {}
    for o in offers:
        listing = _listing_of_offer(o)
        if listing:
            by_host.setdefault(listing[0], {}).setdefault(listing[1], {"offers": [], "seeds": []})["offers"].append(o)
    for s in seeds:
        listing = _listing_of_seed(s)
        if listing:
            by_host.setdefault(listing[0], {}).setdefault(listing[1], {"offers": [], "seeds": []})["seeds"].append(s)
    current = content_listing(row.get("canonical_url"))
    groups = []
    for host, listings in sorted(by_host.items()):
        if len(listings) < 2:
            continue
        keeper = elect_listing_keeper(
            [evidence(h, v["offers"], v["seeds"]) for h, v in sorted(listings.items())], market=market,
            current=current[1] if current and current[0] == host else None)
        k = keeper["handle"]
        mine = {"offers": [o for o in listings[k]["offers"] if _in_market(o, market)],
                "seeds": [x for x in listings[k]["seeds"] if _in_market(x, market)]}
        others = {h: {"offers": [o for o in v["offers"] if _in_market(o, market)],
                      "seeds": [x for x in v["seeds"] if _in_market(x, market)]}
                  for h, v in listings.items() if h != k}
        other_market_left = sum(1 for v in listings.values() for o in v["offers"] + v["seeds"]
                                if not _in_market(o, market))
        live_others = [o for v in others.values() for o in v["offers"] if o.get("suppressed_at") is None]
        suppress = [o["offer_id"] for o in live_others]
        # what a revert puts back: a live offer's metadata (NULL stays NULL)
        prior_metadata = {o["offer_id"]: (_json(o["suppression_metadata"]) if o.get("suppression_metadata") is not None
                                          else None) for o in live_others}
        seeds_off = [x for v in others.values() for x in v["seeds"] if x.get("status") == "active"]
        deactivate = [x["id"] for x in seeds_off]
        live_shelves = {(o["sku_key"], o["channel"], o.get("market")) for o in mine["offers"]
                        if o.get("suppressed_at") is None}
        # The offer whose PRICE was read most recently comes back (review 2: the reconciler bumps updated_at when
        # it suppresses, and the price refresh skips suppressed offers, so updated_at says nothing of the price).
        restore, taken = [], set(live_shelves)
        # (reverse order: a price never read sorts "" -- last)
        for o in sorted(mine["offers"], key=lambda o: (str(o.get("price_checked_at") or ""),
                                                       str(o.get("updated_at") or ""), o["offer_id"]), reverse=True):
            shelf = (o["sku_key"], o["channel"], o.get("market"))
            if o.get("suppressed_at") is not None and o.get("suppression_reason") == DUPLICATE_REASON \
                    and shelf not in taken:
                taken.add(shelf)
                restore.append({"offer_id": o["offer_id"], "prior_suppressed_at": str(o["suppressed_at"]),
                                "price_checked_at": None if o.get("price_checked_at") is None
                                else str(o["price_checked_at"])})
        entry: Dict[str, Any] = {
            "product_key": row["product_key"], "content_key": row.get("content_key"), "host": host,
            "keeper": k, "keeper_evidence": keeper, "left_out": sorted(others), "suppress": suppress,
            "suppress_prior_metadata": prior_metadata,
            "deactivate_prior_updated_at": {x["id"]: None if x.get("updated_at") is None else str(x["updated_at"])
                                            for x in seeds_off},
            "other_market_left": other_market_left,
            "restore": restore, "deactivate": deactivate, "repoint": None, "skipped": None}
        if keeper.get("available") is False:
            # every listing of this host is gone or sold out: moving the row between dead pages (and restoring an
            # offer on one) helps nobody -- the liveness sweep owns these rows
            entry["skipped"] = "no_available_listing"
        elif not live_shelves and not restore:
            entry["skipped"] = "keeper_has_no_sellable_offer"
        elif current and current[0] == host and current[1] != k:
            url = next((str(o.get("canonical_url") or o.get("destination_url") or "") for o in mine["offers"]
                        if o.get("canonical_url") or o.get("destination_url")), "") or next(
                (str(s.get("canonical_url") or s.get("destination_url") or "") for s in mine["seeds"]), "")
            image = next((str(o.get("image_url")) for o in mine["offers"] if o.get("image_url")), "") or next(
                (str(s.get("image_url")) for s in mine["seeds"] if s.get("image_url")), "")
            if url and image:
                entry["repoint"] = {"prior_canonical_url": row.get("canonical_url"),
                                    "prior_image_url": row.get("image_url"), "canonical_url": url, "image_url": image}
            else:
                # half a repair would leave the row naming one page while it sells another: leave it whole
                entry["skipped"] = "keeper_page_unknown"
                entry["repoint_unknown"] = {"canonical_url": bool(url), "image_url": bool(image)}
        if not entry["skipped"] and not (suppress or restore or deactivate or entry["repoint"]):
            entry["skipped"] = "nothing_to_change"  # already one live listing, the row names it, no stray seed
        groups.append(entry)
    return groups


async def plan(db: Any, *, hosts: List[str], keys: List[str], market: str = "US") -> Dict[str, Any]:
    rows = [dict(r) for r in await db.fetch_all(ROWS_SQL, {"source_system": SOURCE_SYSTEM, "keys": keys or None})]
    all_keys = [r["product_key"] for r in rows]
    offers: Dict[str, list] = {}
    seeds: Dict[str, list] = {}
    for i in range(0, len(all_keys), 1000):
        chunk = all_keys[i:i + 1000]
        for o in await db.fetch_all(OFFERS_SQL, {"keys": chunk}):
            offers.setdefault(o["product_key"], []).append(dict(o))
        for s in await db.fetch_all(SEEDS_SQL, {"keys": chunk}):
            seeds.setdefault(s["product_key"], []).append(dict(s))
    wanted = {h.lower().removeprefix("www.") for h in hosts}
    groups = []
    for row in rows:
        for g in plan_row(row, offers.get(row["product_key"], []), seeds.get(row["product_key"], []), market=market):
            if not wanted or g["host"] in wanted:
                groups.append(g)
    return {"market": market, "hosts": sorted(wanted), "rows_read": len(rows), "groups": groups}


def summary(p: Mapping[str, Any]) -> Dict[str, Any]:
    act = [g for g in p["groups"] if not g["skipped"]]
    return {
        "market": p["market"], "hosts": p["hosts"], "rows_read": p["rows_read"],
        "groups": len(p["groups"]), "rows": len({g["product_key"] for g in p["groups"]}),
        "skipped": dict(Counter(g["skipped"] for g in p["groups"] if g["skipped"])),
        "offers_to_suppress": sum(len(g["suppress"]) for g in act),
        "offers_to_restore": sum(len(g["restore"]) for g in act),
        "seeds_to_deactivate": sum(len(g["deactivate"]) for g in act),
        "rows_to_repoint": sum(1 for g in act if g["repoint"]),
        "listings_left_out": sum(len(g["left_out"]) for g in act),
        "other_market_left": sum(g.get("other_market_left") or 0 for g in p["groups"]),
        # how old the price of each offer that comes back is (review 2): by the day its price was last read
        "restored_price_checked": dict(Counter((r["price_checked_at"] or "never")[:10]
                                               for g in act for r in g["restore"]).most_common(15)),
        "by_host": dict(Counter(g["host"] for g in act).most_common(40)),
        "sample": [{k: g[k] for k in ("product_key", "host", "keeper", "left_out", "suppress", "restore",
                                      "repoint", "skipped")} for g in p["groups"][:SAMPLE]],
    }


async def _record(db: Any, action: str, run_id: str, detail: Mapping[str, Any]) -> None:
    row = await db.fetch_one(EVENT_SQL, {"action": action, "run_id": run_id,
                                         "detail": json.dumps(detail, default=str)})
    if not row:
        raise RuntimeError(f"{action} for {run_id} was not recorded")


async def load_manifest(db: Any, run_id: str) -> Dict[str, Any]:
    row = await db.fetch_one(LOAD_MANIFEST_SQL, {"action": MANIFEST_ACTION, "run_id": run_id})
    detail = row and _json(row["detail"])
    if not detail:
        raise SystemExit(f"no stored manifest for {run_id}")
    return detail


def _expect(got: List[Any], ids: List[Any], what: str) -> None:
    if len(got) != len(ids):
        raise RuntimeError(f"drift: {len(ids) - len(got)} of {len(ids)} {what} changed since the plan; "
                           f"nothing written")


async def _forward(db: Any, m: Mapping[str, Any]) -> Dict[str, int]:
    """All or nothing, inside the caller's transaction."""
    done = Counter()
    for g in m["groups"]:
        if g["suppress"]:
            meta = json.dumps({"same_listing_run": m["run_id"], "same_listing_keeper": g["keeper"]})
            got = await db.fetch_all(SUPPRESS_SQL, {"ids": g["suppress"], "reason": REASON, "metadata": meta})
            _expect(got, g["suppress"], "live offers to suppress")
            done["suppressed"] += len(got)
    for g in m["groups"]:
        ids = [r["offer_id"] for r in g["restore"]]
        if not ids:
            continue
        clash = [dict(r) for r in await db.fetch_all(SHELF_TAKEN_SQL, {"ids": ids})]
        if clash:
            raise SystemExit(f"refused: restoring {clash[0]['offer_id']} would duplicate live offer "
                             f"{clash[0]['live_offer_id']} on its shelf; nothing written")
        got = await db.fetch_all(RESTORE_SQL, {"ids": ids, "reason": DUPLICATE_REASON})
        _expect(got, ids, f"{DUPLICATE_REASON} offers to restore")
        done["restored"] += len(got)
    for g in m["groups"]:
        if g["deactivate"]:
            got = await db.fetch_all(SEED_STATUS_SQL, {"ids": g["deactivate"], "status": "inactive", "prior": "active"})
            _expect(got, g["deactivate"], "active seeds to deactivate")
            done["deactivated"] += len(got)
    for g in m["groups"]:
        if g["repoint"]:
            got = await db.fetch_all(REPOINT_SQL, {"product_key": g["product_key"], **g["repoint"]})
            _expect(got, [g["product_key"]], "row URLs to re-point")
            done["repointed"] += 1
    return dict(done)


async def _reverse(db: Any, m: Mapping[str, Any]) -> Dict[str, int]:
    """The forward write, undone value by value; a value no longer at what this run wrote is drift."""
    done = Counter()
    for g in m["groups"]:
        if g["repoint"]:
            r = g["repoint"]
            got = await db.fetch_all(REPOINT_SQL, {
                "product_key": g["product_key"], "canonical_url": r["prior_canonical_url"],
                "image_url": r["prior_image_url"], "prior_canonical_url": r["canonical_url"],
                "prior_image_url": r["image_url"]})
            _expect(got, [g["product_key"]], "re-pointed rows")
            done["repointed"] += 1
        for seed_id in g["deactivate"]:
            got = await db.fetch_all(SEED_RESTORE_SQL, {
                "id": seed_id, "status": "active", "prior": "inactive",
                "prior_updated_at": (g.get("deactivate_prior_updated_at") or {}).get(seed_id)})
            _expect(got, [seed_id], "deactivated seeds")
            done["reactivated"] += 1
        for r in g["restore"]:
            got = await db.fetch_all(RESUPPRESS_SQL, {"offer_id": r["offer_id"], "reason": DUPLICATE_REASON,
                                                      "prior_suppressed_at": r["prior_suppressed_at"]})
            _expect(got, [r["offer_id"]], "restored offers")
            done["resuppressed"] += 1
        for offer_id in g["suppress"]:
            prior = g["suppress_prior_metadata"].get(offer_id)
            got = await db.fetch_all(UNSUPPRESS_SQL, {
                "offer_id": offer_id, "reason": REASON, "run_id": m["run_id"],
                "prior_metadata": None if prior is None else json.dumps(prior)})
            _expect(got, [offer_id], "suppressed offers")
            done["unsuppressed"] += 1
    return dict(done)


async def refresh_after(db: Any, m: Mapping[str, Any], *, source: str) -> Dict[str, Any]:
    """After the commit: each touched content_key's served view and eligibility. Never undoes the write."""
    from services.agent_pdp_view_assembler import refresh_agent_pdp_view_for_content_key
    from services.index_pipeline_state_service import recompute_serving_eligibility

    out: Dict[str, Any] = {"refreshed": 0, "recomputed": 0, "failed_keys": []}
    for ck in sorted({g["content_key"] for g in m["groups"] if g.get("content_key")}):
        try:
            view = await refresh_agent_pdp_view_for_content_key(ck, refresh_source=source, db=db)
            out["refreshed"] += int(bool(view))
            await recompute_serving_eligibility(ck, reason=source, db=db)
            out["recomputed"] += 1
        except Exception as exc:  # noqa: BLE001 -- listed; a cache rebuild never undoes a committed repair
            out["failed_keys"].append({"content_key": ck, "error": f"{type(exc).__name__}: {exc}"[:200]})
    out["failed_keys_total"] = len(out["failed_keys"])
    out["failed_keys"] = out["failed_keys"][:SAMPLE]
    return out


async def apply(db: Any, p: Mapping[str, Any]) -> Dict[str, Any]:
    groups = [g for g in p["groups"] if not g["skipped"]]
    if not groups:
        raise SystemExit("nothing to repair")
    run_id = f"samelisting_{uuid.uuid4().hex[:12]}"
    m = {"run_id": run_id, "market": p["market"], "hosts": p["hosts"], "groups": groups}
    await _record(db, MANIFEST_ACTION, run_id, m)  # committed on its own, BEFORE the write
    async with db.transaction():
        done = await _forward(db, m)
        await _record(db, APPLIED_ACTION, run_id, done)
    print(f"COMMITTED {run_id}: {json.dumps(done)}", flush=True)
    return {"run_id": run_id, **done, "refresh": await refresh_after(db, m, source=run_id)}


def _touched(m: Mapping[str, Any]) -> set:
    return ({("row", g["product_key"]) for g in m["groups"]}
            | {("seed", i) for g in m["groups"] for i in g["deactivate"]}
            | {("offer", i) for g in m["groups"] for i in g["suppress"] + [r["offer_id"] for r in g["restore"]]})


async def revert(db: Any, run_id: str) -> Dict[str, Any]:
    m = await load_manifest(db, run_id)
    state = {r["action"]: r["id"] for r in await db.fetch_all(
        RUN_STATE_SQL, {"run_id": run_id, "actions": [APPLIED_ACTION, REVERTED_ACTION]})}
    if APPLIED_ACTION not in state:
        raise SystemExit(f"{run_id} never committed: nothing to revert")
    if REVERTED_ACTION in state:
        raise SystemExit(f"{run_id} is already reverted")
    mine = _touched(m)
    for r in await db.fetch_all(LATER_LIVE_RUNS_SQL, {"applied": APPLIED_ACTION, "reverted": REVERTED_ACTION,
                                                      "after_id": state[APPLIED_ACTION]}):
        shared = mine & _touched(await load_manifest(db, r["run_id"]))
        if shared:
            raise SystemExit(f"refused: later run {r['run_id']} touched {len(shared)} of these rows; revert it first")
    async with db.transaction():
        done = await _reverse(db, m)
        await _record(db, REVERTED_ACTION, run_id, done)
    print(f"REVERTED {run_id}: {json.dumps(done)}", flush=True)
    return {"run_id": run_id, **done, "refresh": await refresh_after(db, m, source=f"{run_id}:revert")}


def exit_code(out: Mapping[str, Any]) -> int:
    """Non-zero when a committed write left views unrebuilt: re-run `refresh --run-id`."""
    return 2 if ((out.get("refresh") or {}).get("failed_keys_total") or 0) > 0 else 0


async def run(args: argparse.Namespace) -> int:
    await database.connect()
    try:
        out: Dict[str, Any] = {}
        if args.command == "revert":
            out = await revert(database, args.run_id)
            print("REVERT_DONE " + json.dumps(out, default=str), flush=True)
        elif args.command == "refresh":
            m = await load_manifest(database, args.run_id)
            out = {"refresh": await refresh_after(database, m, source=args.run_id)}
            print("REFRESHED " + json.dumps(out, default=str), flush=True)
        else:
            p = await plan(database, hosts=args.host, keys=args.product_key, market=args.market)
            print("PLAN " + json.dumps(summary(p), default=str), flush=True)
            if args.apply:
                out = await apply(database, p)
                print("APPLIED " + json.dumps(out, default=str), flush=True)
            else:
                print("dry run: nothing written (--apply to write)", flush=True)
        return exit_code(out)
    finally:
        await database.disconnect()


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", nargs="?", default="plan", choices=["plan", "revert", "refresh"])
    p.add_argument("--host", action="append", default=[])
    p.add_argument("--product-key", dest="product_key", action="append", default=[])
    p.add_argument("--market", default="US")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--run-id", dest="run_id")
    args = p.parse_args(argv)
    if args.command in ("revert", "refresh") and not args.run_id:
        p.error(f"{args.command} needs --run-id")
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
