#!/usr/bin/env python3
"""Restamp the DECLARED market of existing offers on named stores: catalog_offers.market FROM -> TO.

WHY. The catalog upserts write `catalog_offers.market` on INSERT only (catalog_enrichment_agent/apply
`_offer_upsert_sql_with_market`, external_offer_dual_write), so offers a lane wrote without declaring a market
keep the column's DEFAULT 'US' -- jsmbeauty.sg and makeupforever.sg: 1,080 SGD offers stamped 'US' (census
2026-09-29) -- and a re-crawl under the retailer_ingest SG market cannot move them: its readback fails
"stamped market SG: 0" (services/retailer_ingest/pipeline.py). services/catalog_invariant_checks counts the
same rows as a market/currency disagreement.

TWO LANES DO REWRITE market on an existing row, and a restamp of their offers would be silently undone, so the
plan REFUSES them (review of #2450):
  * catalog_sync (catalog_track 'internal_merchant'): `_upsert_by_pk` rewrites every column on re-sync;
  * scripts/onboard_external_brand_from_crawl.py (seed id `external_brand_crawl::<epid>`): sets market from
    the seed, whose partition is 'US'.

WHAT IT CHANGES for a buyer: region pricing gates on CURRENCY, never on market (services/region_pricing.py),
so serving eligibility does not move. offer_buyability compares the offer's market to the request's: after
the restamp an SG buyer sees these offers as domestic, a US buyer as cross-border. agent_pdp_view copies
o.market, so every touched content_key's view is rebuilt after the commit. `updated_at` is NOT bumped: a
restamp is not a new observation, and `ORDER BY o.updated_at` readers (pivot_query_service, the reconciler's
keeper election) must not reorder over it -- so the nightly view reconciler will not retry a failed rebuild
either: the run exits non-zero and `refresh --run-id` is the retry.

SCOPE -- a restamp, never a relabel of price:
  * only offers on the named hosts (offer source_domain, else its product's canonical_url; scheme, userinfo,
    port, path, `www.` and a trailing dot folded -- `offer_host` below and HOST_SQL agree); rows whose raw
    source text names a host but does not normalise to it are REPORTED as near misses, never written;
  * only rows stamped FROM (any spelling: trimmed, upper-cased) whose currency is ALREADY the TO market's --
    a USD sibling on the same store (Shopify-Markets capture) is never touched;
  * suppressed rows included: relabelling a row that does not serve changes nothing a buyer sees, and a row
    left on the wrong market would re-enter wrong the day its suppression lifts (the currency backfill's
    lesson, scripts/backfill_offer_market_currency.py);
  * refused whole when the target would duplicate a live (sku_key, channel, market) shelf -- against existing
    offers AND among the planned ones -- checked at plan time and again inside the write's transaction, in
    both directions (a revert must not collide with a US offer written since).

  Dry run (default; writes nothing):
    python -m scripts.restamp_offer_market --host jsmbeauty.sg --host makeupforever.sg --from US --to SG
  Apply (the manifest is STORED in identity_resolution_events before the write; one transaction; any drift
  since the plan aborts it all; the run id is printed the moment it commits):
    ... --apply
  Revert a run (all or nothing; refused while a LATER applied run holds any of its offers), or re-run only its
  view / eligibility rebuild:
    python -m scripts.restamp_offer_market revert --run-id restamp_<hex>
    python -m scripts.restamp_offer_market refresh --run-id restamp_<hex> [--reverse]

Needs DATABASE_URL: run it through scripts/ops/run_oneoff_job.sh.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import uuid
from collections import Counter
from typing import Any, Dict, List, Mapping, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.database import database  # noqa: E402
from services.region_pricing import normalize_region, pricing_currency_for_region  # noqa: E402

REWRITING_TRACKS = ("internal_merchant",)       # catalog_sync_service._upsert_by_pk rewrites every column
# onboard_external_brand_from_crawl sets market from the seed. Its seeds carry tool '*' (SEED_TOOL_SCOPE, since
# #2169); the provenance is the id prefix `external_brand_crawl::<epid>` (its _seed_id) -- pinned against the
# producer by the tests (review 2 of #2450: a tool = 'external_brand_crawl' match found none of its seeds).
REWRITING_SEED_ID_PREFIXES = ("external_brand_crawl::",)


def _host_sql(expr: str) -> str:
    """`offer_host` in SQL: lower/trim, drop an http(s) scheme (or a bare //), userinfo, then everything from the
    first / ? # or : (path, query, fragment, port), then any leading www. and trailing dots. Text with another
    scheme (mailto:...), a backslash, or a result that is not a hostname names no web host: ''."""
    t = f"lower(trim(coalesce({expr}, '')))"
    s = f"regexp_replace({t}, '^(https?:)?//', '')"
    s = f"regexp_replace({s}, '^[^/?#@]*@', '')"
    s = f"regexp_replace({s}, '[/?#:].*$', '')"
    s = f"regexp_replace(regexp_replace({s}, '^(www[.])+', ''), '[.]+$', '')"
    return (f"(CASE WHEN strpos({t}, chr(92)) > 0 OR ({t} ~ '^[a-z][a-z0-9+.-]*:[^0-9]' AND {t} !~ '^https?:') "
            f"OR {s} !~ '^[a-z0-9.-]*$' THEN '' ELSE {s} END)")


_SOURCE_TEXT = "coalesce(nullif(trim(o.source_domain), ''), cp.canonical_url)"
HOST_SQL = _host_sql(_SOURCE_TEXT)
MARKET_SQL = "upper(trim(coalesce({a}.market, '')))"
CURRENCY_SQL = "upper(trim(coalesce({a}.currency, '')))"

PLAN_SQL = f"""
SELECT o.offer_id, o.product_key, o.sku_key, o.channel, o.market, o.currency, o.catalog_track, o.source_system,
       (o.suppressed_at IS NULL) AS live, cp.content_key, {HOST_SQL} AS host,
       EXISTS (SELECT 1 FROM external_product_seeds s WHERE s.id = cp.source_ref
                 AND s.id LIKE ANY(:rewriting_id_patterns)) AS seed_lane_rewrites
FROM catalog_offers o LEFT JOIN catalog_products cp ON cp.product_key = o.product_key
WHERE {HOST_SQL} = ANY(:hosts)
  AND {MARKET_SQL.format(a='o')} = :from_market
  AND {CURRENCY_SQL.format(a='o')} = :currency
ORDER BY o.offer_id
"""

# Everything else on those hosts, so the report says what was LEFT and why (a USD sibling, another market).
LEFT_ALONE_SQL = f"""
SELECT {HOST_SQL} AS host, {MARKET_SQL.format(a='o')} AS market, {CURRENCY_SQL.format(a='o')} AS currency,
       count(*) AS n
FROM catalog_offers o LEFT JOIN catalog_products cp ON cp.product_key = o.product_key
WHERE {HOST_SQL} = ANY(:hosts)
  AND NOT ({MARKET_SQL.format(a='o')} = :from_market AND {CURRENCY_SQL.format(a='o')} = :currency)
GROUP BY 1, 2, 3 ORDER BY 1, 4 DESC
"""

# Rows whose source text MENTIONS a named host but does not normalise to it (a subdomain, a lookalike, a URL
# the normaliser cannot read): never written, always shown -- "nothing left alone" must not be blind to them.
NEAR_MISS_SQL = f"""
SELECT o.offer_id, {_SOURCE_TEXT} AS source_text, {HOST_SQL} AS host
FROM catalog_offers o LEFT JOIN catalog_products cp ON cp.product_key = o.product_key
WHERE {_SOURCE_TEXT} ILIKE ANY(:host_patterns) AND NOT ({HOST_SQL} = ANY(:hosts))
  AND {CURRENCY_SQL.format(a='o')} = :currency
ORDER BY o.offer_id LIMIT 50
"""

# A move of `ids` to `target` duplicates a live shelf when another live offer already sits on its
# (sku_key, channel) in `target`, or when two live offers of the move share one.
COLLISIONS_SQL = f"""
SELECT o.offer_id, s.offer_id AS existing_offer_id, o.sku_key, o.channel
FROM catalog_offers o
JOIN catalog_offers s ON s.sku_key = o.sku_key AND s.channel = o.channel AND s.offer_id <> o.offer_id
WHERE o.offer_id = ANY(:ids) AND s.suppressed_at IS NULL
  AND ({MARKET_SQL.format(a='s')} = :target OR (s.offer_id = ANY(:ids) AND o.suppressed_at IS NULL))
ORDER BY o.offer_id, s.offer_id
"""

# A revert leaves an offer that already reads its prior value where it is (review 2 of #2450: after a later run
# B restamped an offer run A had moved, and B was reverted, that offer is back at A's prior value -- counting it
# as drift made A unrevertable forever).
ALREADY_AT_SQL = """
SELECT offer_id FROM catalog_offers WHERE offer_id = ANY(:ids) AND market = :target
"""

# Drift-guarded on the exact value read at plan time; the count check in `_write` aborts the transaction.
RESTAMP_SQL = f"""
UPDATE catalog_offers SET market = :to_market
WHERE offer_id = ANY(:ids) AND market = :prior_market AND {CURRENCY_SQL.format(a='catalog_offers')} = :currency
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
SELECT action, id FROM identity_resolution_events WHERE run_id = :run_id AND action = ANY(:actions)
"""
LOAD_MANIFEST_SQL = """
SELECT detail FROM identity_resolution_events WHERE action = :action AND run_id = :run_id
ORDER BY id DESC LIMIT 1
"""
# Runs applied after `after_id` and not reverted: a revert of an older run must not move their offers.
LATER_LIVE_RUNS_SQL = """
SELECT a.run_id FROM identity_resolution_events a
WHERE a.action = :applied AND a.id > :after_id
  AND NOT EXISTS (SELECT 1 FROM identity_resolution_events r WHERE r.run_id = a.run_id AND r.action = :reverted)
"""

# A job log cuts a line at ~100 KB: the report carries counts and a bounded sample.
SAMPLE = 20

_SCHEME = re.compile(r"^(https?:)?//")


def offer_host(text: Any) -> str:
    """HOST_SQL, in Python (the two are pinned equal by the tests)."""
    t = str(text or "").strip().lower()
    s = _SCHEME.sub("", t)
    s = re.sub(r"^[^/?#@]*@", "", s)
    s = re.sub(r"[/?#:].*$", "", s)
    s = re.sub(r"[.]+$", "", re.sub(r"^(www[.])+", "", s))
    other_scheme = re.match(r"^[a-z][a-z0-9+.-]*:[^0-9]", t) and not re.match(r"^https?:", t)
    return "" if "\\" in t or other_scheme or not re.fullmatch(r"[a-z0-9.-]*", s) else s


async def collisions(db: Any, ids: List[str], target: str) -> List[Dict[str, Any]]:
    if not ids:
        return []
    return [dict(r) for r in await db.fetch_all(COLLISIONS_SQL, {"ids": ids, "target": target.strip().upper()})]


async def plan(db: Any, hosts: List[str], from_market: str, to_market: str) -> Dict[str, Any]:
    from_m, to_m = normalize_region(from_market), normalize_region(to_market)
    if from_m == to_m:
        raise ValueError(f"from and to are both {to_m}")
    currency = pricing_currency_for_region(to_m)  # raises for a market with no pricing currency
    hosts = sorted({offer_host(h) for h in hosts if offer_host(h)})
    if not hosts:
        raise ValueError("at least one --host is required")
    values = {"hosts": hosts, "from_market": from_m, "currency": currency}
    rows = [dict(r) for r in await db.fetch_all(PLAN_SQL, {**values, "rewriting_id_patterns": [f"{p}%" for p in REWRITING_SEED_ID_PREFIXES]})]
    left = [dict(r) for r in await db.fetch_all(LEFT_ALONE_SQL, values)]
    near = [dict(r) for r in await db.fetch_all(NEAR_MISS_SQL, {
        "hosts": hosts, "currency": currency, "host_patterns": [f"%{h}%" for h in hosts]})]
    rewriting = [r["offer_id"] for r in rows
                 if (r["catalog_track"] or "") in REWRITING_TRACKS or r["seed_lane_rewrites"]]
    return {"hosts": hosts, "from_market": from_m, "to_market": to_m, "currency": currency,
            "offers": [{"offer_id": r["offer_id"], "prior_market": r["market"], "content_key": r["content_key"],
                        "host": r["host"], "live": bool(r["live"])} for r in rows],
            "lanes": dict(Counter(f"{r['source_system']}|{r['catalog_track']}" for r in rows)),
            "rewriting_lane_offers": rewriting,
            "left_alone": left, "near_misses": near,
            "collisions": await collisions(db, [r["offer_id"] for r in rows], to_m)}


def summary(p: Mapping[str, Any]) -> Dict[str, Any]:
    offers = p["offers"]
    by = Counter((o["host"], "live" if o["live"] else "suppressed") for o in offers)
    return {
        "hosts": p["hosts"], "from": p["from_market"], "to": p["to_market"], "currency": p["currency"],
        "offers": len(offers),
        "by_host": {f"{h} {state}": n for (h, state), n in sorted(by.items())},
        "lanes": p["lanes"],
        "rewriting_lane_offers": len(p["rewriting_lane_offers"]),
        "content_keys": len({o["content_key"] for o in offers if o["content_key"]}),
        "offers_without_content_key": sum(1 for o in offers if not o["content_key"]),
        "prior_market_spellings": dict(Counter(o["prior_market"] for o in offers)),
        "left_alone": p["left_alone"],
        "near_misses": len(p["near_misses"]), "near_miss_sample": p["near_misses"][:SAMPLE],
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


async def _write(db: Any, m: Mapping[str, Any], *, reverse: bool) -> Dict[str, int]:
    """All or nothing, inside the caller's transaction: every offer moves from its read value to its target, or
    none does. Forward: prior spelling -> TO. Reverse: TO -> that offer's own prior spelling, leaving an offer
    that already reads it (`already`)."""
    groups: Dict[tuple, List[str]] = {}
    for o in m["offers"]:
        pair = (m["to_market"], o["prior_market"]) if reverse else (o["prior_market"], m["to_market"])
        groups.setdefault(pair, []).append(o["offer_id"])
    moved = already = 0
    for (prior, target), ids in groups.items():
        if reverse:
            there = {r["offer_id"] for r in await db.fetch_all(ALREADY_AT_SQL, {"ids": ids, "target": target})}
            ids, already = [i for i in ids if i not in there], already + len(there)
            if not ids:
                continue
        clash = await collisions(db, ids, target)  # again, inside the transaction: offers written since the plan
        if clash:
            raise SystemExit(f"refused: {len(clash)} offer(s) would duplicate a live {target.strip().upper()} "
                             f"shelf on the same (sku_key, channel), e.g. {clash[0]}; nothing written")
        got = await db.fetch_all(RESTAMP_SQL, {"ids": ids, "to_market": target, "prior_market": prior,
                                               "currency": m["currency"]})
        if len(got) != len(ids):
            raise RuntimeError(f"drift: {len(ids) - len(got)} of {len(ids)} offer(s) no longer read market "
                               f"{prior!r} in {m['currency']}; nothing written")
        moved += len(got)
    return {"moved": moved, "already": already}


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
    if p["rewriting_lane_offers"]:
        raise SystemExit(f"refused: {len(p['rewriting_lane_offers'])} offer(s) belong to a lane that rewrites "
                         f"market on re-sync (e.g. {p['rewriting_lane_offers'][0]}); nothing written")
    if p["collisions"]:
        raise SystemExit(f"refused: {len(p['collisions'])} offer(s) would duplicate a live {p['to_market']} "
                         f"shelf on the same (sku_key, channel); nothing written")
    if not p["offers"]:
        raise SystemExit("nothing to restamp")
    run_id = f"restamp_{uuid.uuid4().hex[:12]}"
    m = {**{k: p[k] for k in ("hosts", "from_market", "to_market", "currency", "offers")}, "run_id": run_id}
    await _record(db, MANIFEST_ACTION, run_id, m)  # committed on its own, BEFORE the write
    async with db.transaction():
        moved = (await _write(db, m, reverse=False))["moved"]
        await _record(db, APPLIED_ACTION, run_id, {"moved": moved})
    # Before the rebuild: a job killed during 245 view rebuilds must still leave the committed run id in its log.
    print(f"COMMITTED {run_id}: {moved} offer(s) restamped {m['from_market']} -> {m['to_market']}", flush=True)
    return {"run_id": run_id, "moved": moved, "refresh": await refresh_after(db, m, source=run_id)}


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
            raise SystemExit(f"refused: later run {r['run_id']} restamped {len(shared)} of these offers; revert "
                             f"it first")
    async with db.transaction():
        done = await _write(db, m, reverse=True)
        await _record(db, REVERTED_ACTION, run_id, done)
    print(f"REVERTED {run_id}: {done['moved']} offer(s), {done['already']} already at their prior market", flush=True)
    return {"run_id": run_id, "reverted": done["moved"], "already": done["already"],
            "refresh": await refresh_after(db, m, source=f"{run_id}:revert")}


def exit_code(out: Mapping[str, Any]) -> int:
    """Non-zero when a committed write left views unrebuilt: nothing else retries them -- the nightly reconciler
    (jobs/agent_pdp_view_reconciler_cron.py) keys "changed" on MAX(offer.updated_at), which a restamp does not
    bump. Re-run `refresh --run-id`."""
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
            src = f"{args.run_id}:revert" if args.reverse else args.run_id
            out = {"refresh": await refresh_after(database, m, source=src)}
            print("REFRESHED " + json.dumps(out, default=str), flush=True)
        else:
            p = await plan(database, args.host, args.from_market, args.to_market)
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
