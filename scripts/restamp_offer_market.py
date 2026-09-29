#!/usr/bin/env python3
"""Restamp the DECLARED market of existing offers on named stores: catalog_offers.market FROM -> TO.

WHY. `catalog_offers.market` is INSERT-only: no offer upsert updates it on conflict (external_offer_dual_write,
catalog_enrichment_agent/apply, reconcile_catalog_offers ...). Offers written by a lane that never declared a
market took the column's DEFAULT 'US' -- jsmbeauty.sg and makeupforever.sg: 1,080 SGD offers stamped 'US'
(census 2026-09-29) -- and a re-crawl under the retailer_ingest SG market cannot move them, so its readback
fails "stamped market SG: 0" (services/retailer_ingest/pipeline.py). services/catalog_invariant_checks counts
the same rows as a market/currency disagreement.

WHAT IT CHANGES for a buyer: region pricing gates on CURRENCY, never on market (services/region_pricing.py),
so serving eligibility does not move. offer_buyability compares the offer's market to the request's: after
the restamp an SG buyer sees these offers as domestic, a US buyer as cross-border. agent_pdp_view copies
o.market, so every touched content_key's view is rebuilt after the commit.

SCOPE -- a restamp, never a relabel of price:
  * only offers on the named hosts (offer source_domain, else its product's canonical_url; www. folded);
  * only rows stamped FROM whose currency is ALREADY the TO market's currency (region_pricing's one rule,
    require_market_currency) -- a USD sibling on the same store (Shopify-Markets capture) is never touched;
  * suppressed rows included: relabelling a row that does not serve changes nothing a buyer sees, and a row
    left on the wrong market would re-enter wrong the day its suppression lifts (the currency backfill's
    lesson, scripts/backfill_offer_market_currency.py);
  * refused whole when any planned offer's (sku_key, channel) already has a live TO offer: the restamp would
    make a duplicate shelf (catalog_invariant_checks duplicate_offers_per_sku_channel_market).

  Dry run (default; writes nothing):
    python -m scripts.restamp_offer_market --host jsmbeauty.sg --host makeupforever.sg --from US --to SG
  Apply (the manifest is STORED in identity_resolution_events before the write; one transaction; any drift
  since the plan aborts it all):
    ... --apply
  Revert a run (all or nothing), or re-run only its view / eligibility rebuild:
    python -m scripts.restamp_offer_market revert --run-id restamp_<hex>
    python -m scripts.restamp_offer_market refresh --run-id restamp_<hex> [--reverse]

A later INSERT by the same lane still takes the DEFAULT 'US': this fixes the rows that exist, not the writer.

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
from services.region_pricing import normalize_region, pricing_currency_for_region  # noqa: E402

# The host an offer belongs to: its own source_domain, else its product's URL. A bare-host source_domain has no
# scheme, so the scheme is optional; www. folds as everywhere else (apply._LEGACY_LISTING_OWNERS_SQL).
HOST_SQL = ("lower(split_part(regexp_replace(coalesce(nullif(o.source_domain, ''), cp.canonical_url), "
            "'^(https?://)?(www[.])?', '', 'i'), '/', 1))")

PLAN_SQL = f"""
SELECT o.offer_id, o.product_key, o.sku_key, o.channel, o.market, o.currency,
       (o.suppressed_at IS NULL) AS live, cp.content_key, {HOST_SQL} AS host
FROM catalog_offers o LEFT JOIN catalog_products cp ON cp.product_key = o.product_key
WHERE {HOST_SQL} = ANY(:hosts)
  AND upper(trim(coalesce(o.market, ''))) = :from_market
  AND upper(trim(coalesce(o.currency, ''))) = :currency
ORDER BY o.offer_id
"""

# Everything else on those hosts, so the report says what was LEFT and why (a USD sibling, another market).
LEFT_ALONE_SQL = f"""
SELECT {HOST_SQL} AS host, upper(trim(coalesce(o.market, ''))) AS market,
       upper(trim(coalesce(o.currency, ''))) AS currency, count(*) AS n
FROM catalog_offers o LEFT JOIN catalog_products cp ON cp.product_key = o.product_key
WHERE {HOST_SQL} = ANY(:hosts)
  AND NOT (upper(trim(coalesce(o.market, ''))) = :from_market AND upper(trim(coalesce(o.currency, ''))) = :currency)
GROUP BY 1, 2, 3 ORDER BY 1, 4 DESC
"""

COLLISIONS_SQL = """
SELECT o.offer_id, s.offer_id AS existing_offer_id, o.sku_key, o.channel
FROM catalog_offers o
JOIN catalog_offers s ON s.sku_key = o.sku_key AND s.channel = o.channel AND s.offer_id <> o.offer_id
WHERE o.offer_id = ANY(:ids) AND s.suppressed_at IS NULL
  AND upper(trim(coalesce(s.market, ''))) = :to_market
ORDER BY o.offer_id
"""

# Drift-guarded on the value read at plan time: an offer whose market or currency moved since is not written,
# and the count check below then aborts the whole transaction.
RESTAMP_SQL = """
UPDATE catalog_offers SET market = :to_market, updated_at = NOW()
WHERE offer_id = ANY(:ids) AND market = :prior_market AND upper(trim(coalesce(currency, ''))) = :currency
RETURNING offer_id
"""

MANIFEST_ACTION = "offer_market_restamp_manifest"
APPLIED_ACTION, REVERTED_ACTION = "offer_market_restamp_applied", "offer_market_restamp_reverted"
EVENT_SQL = """
INSERT INTO identity_resolution_events (proposal_id, action, run_id, detail)
VALUES (NULL, :action, :run_id, CAST(:detail AS jsonb))
RETURNING id
"""
RUN_STATE_SQL = """
SELECT action FROM identity_resolution_events WHERE run_id = :run_id AND action = ANY(:actions)
"""
LOAD_MANIFEST_SQL = """
SELECT detail FROM identity_resolution_events WHERE action = :action AND run_id = :run_id
ORDER BY id DESC LIMIT 1
"""

# A job log cuts a line at ~100 KB: the report carries counts and a bounded sample.
SAMPLE = 20


def normalize_host(host: str) -> str:
    h = str(host or "").strip().lower()
    h = h.split("://", 1)[-1].split("/", 1)[0]
    return h[4:] if h.startswith("www.") else h


async def plan(db: Any, hosts: List[str], from_market: str, to_market: str) -> Dict[str, Any]:
    from_m, to_m = normalize_region(from_market), normalize_region(to_market)
    if from_m == to_m:
        raise ValueError(f"from and to are both {to_m}")
    currency = pricing_currency_for_region(to_m)  # raises for a market with no pricing currency
    hosts = sorted({normalize_host(h) for h in hosts if normalize_host(h)})
    if not hosts:
        raise ValueError("at least one --host is required")
    values = {"hosts": hosts, "from_market": from_m, "currency": currency}
    rows = [dict(r) for r in await db.fetch_all(PLAN_SQL, values)]
    left = [dict(r) for r in await db.fetch_all(LEFT_ALONE_SQL, values)]
    ids = [r["offer_id"] for r in rows]
    collisions = [dict(r) for r in await db.fetch_all(COLLISIONS_SQL, {"ids": ids, "to_market": to_m})] if ids else []
    return {"hosts": hosts, "from_market": from_m, "to_market": to_m, "currency": currency,
            "offers": [{"offer_id": r["offer_id"], "prior_market": r["market"], "content_key": r["content_key"],
                        "host": r["host"], "live": bool(r["live"])} for r in rows],
            "left_alone": left, "collisions": collisions}


def summary(p: Mapping[str, Any]) -> Dict[str, Any]:
    offers = p["offers"]
    by = Counter((o["host"], "live" if o["live"] else "suppressed") for o in offers)
    return {
        "hosts": p["hosts"], "from": p["from_market"], "to": p["to_market"], "currency": p["currency"],
        "offers": len(offers),
        "by_host": {f"{h} {state}": n for (h, state), n in sorted(by.items())},
        "content_keys": len({o["content_key"] for o in offers if o["content_key"]}),
        "offers_without_content_key": sum(1 for o in offers if not o["content_key"]),
        "prior_market_spellings": dict(Counter(o["prior_market"] for o in offers)),
        "left_alone": p["left_alone"],
        "collisions": len(p["collisions"]), "collision_sample": p["collisions"][:SAMPLE],
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


async def _write(db: Any, m: Mapping[str, Any], *, reverse: bool) -> int:
    """All or nothing, inside the caller's transaction: every offer moves from its read value, or none does."""
    to_m = m["from_market"] if reverse else m["to_market"]
    groups: Dict[str, List[str]] = {}
    for o in m["offers"]:
        prior = m["to_market"] if reverse else o["prior_market"]
        target = o["prior_market"] if reverse else to_m
        groups.setdefault(f"{prior}\x00{target}", []).append(o["offer_id"])
    moved = 0
    for key, ids in groups.items():
        prior, target = key.split("\x00")
        got = await db.fetch_all(RESTAMP_SQL, {"ids": ids, "to_market": target, "prior_market": prior,
                                               "currency": m["currency"]})
        if len(got) != len(ids):
            raise RuntimeError(f"drift: {len(ids) - len(got)} of {len(ids)} offer(s) no longer read market "
                               f"{prior!r} in {m['currency']}; nothing written")
        moved += len(got)
    return moved


async def refresh_after(db: Any, m: Mapping[str, Any], *, source: str) -> Dict[str, Any]:
    """After the commit: each touched content_key's served view (it copies o.market) and its eligibility. Never
    undoes the write; failures are listed so `refresh --run-id` can re-run just this."""
    from services.agent_pdp_view_assembler import refresh_agent_pdp_view_for_content_key
    from services.index_pipeline_state_service import recompute_serving_eligibility

    out: Dict[str, Any] = {"refreshed": 0, "recomputed": 0, "failed_keys": []}
    for ck in sorted({o["content_key"] for o in m["offers"] if o.get("content_key")}):
        try:
            out["refreshed"] += int(bool(await refresh_agent_pdp_view_for_content_key(ck, refresh_source=source, db=db)))
            await recompute_serving_eligibility(ck, reason=source, db=db)
            out["recomputed"] += 1
        except Exception as exc:  # noqa: BLE001 -- listed; a cache rebuild never undoes a committed restamp
            out["failed_keys"].append({"content_key": ck, "error": f"{type(exc).__name__}: {exc}"[:200]})
    out["failed_keys_total"] = len(out["failed_keys"])
    out["failed_keys"] = out["failed_keys"][:SAMPLE]
    return out


async def apply(db: Any, p: Mapping[str, Any]) -> Dict[str, Any]:
    if p["collisions"]:
        raise SystemExit(f"refused: {len(p['collisions'])} offer(s) would duplicate a live {p['to_market']} "
                         f"offer on the same (sku_key, channel); nothing written")
    if not p["offers"]:
        raise SystemExit("nothing to restamp")
    run_id = f"restamp_{uuid.uuid4().hex[:12]}"
    m = {**{k: p[k] for k in ("hosts", "from_market", "to_market", "currency", "offers")}, "run_id": run_id}
    await _record(db, MANIFEST_ACTION, run_id, m)  # committed on its own, BEFORE the write
    async with db.transaction():
        moved = await _write(db, m, reverse=False)
        await _record(db, APPLIED_ACTION, run_id, {"moved": moved})
    return {"run_id": run_id, "moved": moved, "refresh": await refresh_after(db, m, source=run_id)}


async def revert(db: Any, run_id: str) -> Dict[str, Any]:
    m = await load_manifest(db, run_id)
    state = {r["action"] for r in await db.fetch_all(RUN_STATE_SQL, {"run_id": run_id,
                                                                    "actions": [APPLIED_ACTION, REVERTED_ACTION]})}
    if APPLIED_ACTION not in state:
        raise SystemExit(f"{run_id} never committed: nothing to revert")
    if REVERTED_ACTION in state:
        raise SystemExit(f"{run_id} is already reverted")
    async with db.transaction():
        moved = await _write(db, m, reverse=True)
        await _record(db, REVERTED_ACTION, run_id, {"moved": moved})
    return {"run_id": run_id, "reverted": moved, "refresh": await refresh_after(db, m, source=f"{run_id}:revert")}


async def run(args: argparse.Namespace) -> int:
    await database.connect()
    try:
        if args.command == "revert":
            print("REVERTED " + json.dumps(await revert(database, args.run_id), default=str), flush=True)
        elif args.command == "refresh":
            m = await load_manifest(database, args.run_id)
            src = f"{args.run_id}:revert" if args.reverse else args.run_id
            print("REFRESHED " + json.dumps(await refresh_after(database, m, source=src), default=str), flush=True)
        else:
            p = await plan(database, args.host, args.from_market, args.to_market)
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
    p.add_argument("command", nargs="?", default="plan", choices=["plan", "revert", "refresh"])
    p.add_argument("--host", action="append", default=[])
    p.add_argument("--from", dest="from_market", default="US")
    p.add_argument("--to", dest="to_market")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--run-id", dest="run_id")
    p.add_argument("--reverse", action="store_true", help="refresh: the run was reverted")
    args = p.parse_args(argv)
    if args.command == "plan" and (not args.host or not args.to_market):
        p.error("plan needs --host (repeatable) and --to")
    if args.command in ("revert", "refresh") and not args.run_id:
        p.error(f"{args.command} needs --run-id")
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
