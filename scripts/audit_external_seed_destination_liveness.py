#!/usr/bin/env python3
"""DETECTIVE: which active external seeds point at a PDP the brand no longer sells?

READ-ONLY REPORTING over the same mechanism the scheduled sweep runs:
`services/external_seed_destination_liveness` owns the catalogue read, the probe, and the
classification, and this script only decides what to fetch and how to print it. It was
deliberately collapsed onto that module rather than keeping its own copy — an audit that
re-implements its subject measures itself, and this one's numbers are quoted in
`docs/external-seed-dead-pdp-link-audit.md`.

WHAT IT MEASURES, in two stages, because the cheap stage is not the truth:

  stage 1  per HOST, read the brand's own `/products.json` and join the seed handles against
           it. One request per 250 products; 44 requests covered 3,951 seeds. It is the
           CANDIDATE FINDER, not evidence — a delisted handle can still render (measured:
           cosrx.com serves 5 of 12 delisted PDPs at 200).
  stage 2  probe each DELISTED PDP and classify what a shopper would actually get.

A HOST WE CANNOT READ IS NOT A HOST WITH DEAD LINKS. `bot_challenge` (213 of 286 hosts from a
non-crawl-egress client) and `incomplete` (a truncated pagination, which once produced 285
fabricated dead handles on one host) are reported as their own outcomes and excluded from
every rate. See the service module for why neither may ever buy a retirement.

It never writes to the database and has no `--apply`; the writer is
`jobs/external_seed_destination_sweep`. Every outbound request goes through
`services.crawl_politeness`.

Usage:

    # from the DB (read-only), full active corpus
    DATABASE_URL=... python3 -m scripts.audit_external_seed_destination_liveness \\
        --out /tmp/seed_liveness.json

    # only some brands, and skip the per-PDP stage
    python3 -m scripts.audit_external_seed_destination_liveness \\
        --host cosrx.com --host mixsoon.us --no-probe
"""
from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from typing import Any, Dict, List, Optional, Sequence, Set

import httpx

from services.external_seed_destination_liveness import (
    ALL_VERDICTS,
    CATALOGUE_OK,
    CONFIRMED_DEAD_VERDICTS,
    classify_destination,
    destination_of,
    group_by_host,
    host_of,
    probe_destination,
    read_brand_catalogue,
)
from services.outbound_warm_handoff import extract_product_handle

__all__ = [
    "classify_destination",
    "group_seeds_by_host",
    "host_of",
    "probe_destination",
    "read_brand_catalogue",
    "summarize",
]


async def load_seeds_from_db() -> List[Dict[str, Any]]:
    from db.database import database  # imported late: --seeds-file needs no DB

    await database.connect()
    try:
        rows = await database.fetch_all(
            """
            SELECT id, status, domain, canonical_url, destination_url,
                   created_at, updated_at, attached_product_key,
                   destination_checked_at, destination_verdict, destination_failure_streak
            FROM external_product_seeds
            WHERE status = 'active'
            """,
            {},
        )
    finally:
        await database.disconnect()
    return [dict(row) for row in rows or []]


def group_seeds_by_host(
    rows: Sequence[Dict[str, Any]], only_hosts: Optional[Set[str]] = None
) -> Dict[str, List[Dict[str, Any]]]:
    """`group_by_host` from the service, plus this script's `--host` filter."""
    grouped = group_by_host(rows)
    if only_hosts:
        grouped = {h: v for h, v in grouped.items() if h in only_hosts}
    return grouped


async def run(args: argparse.Namespace) -> Dict[str, Any]:
    rows = json.load(open(args.seeds_file)) if args.seeds_file else await load_seeds_from_db()
    by_host = group_seeds_by_host(rows, set(args.host) if args.host else None)
    hosts = sorted(by_host, key=lambda h: -len(by_host[h]))
    handle_bearing = sum(len(v) for v in by_host.values())
    print(
        f"{len(rows)} active seeds -> {handle_bearing} with a /products/<handle> "
        f"across {len(hosts)} hosts",
        flush=True,
    )

    sem = asyncio.Semaphore(args.host_concurrency)
    results: Dict[str, Dict[str, Any]] = {}
    done = 0

    async with httpx.AsyncClient(timeout=args.timeout, follow_redirects=True) as client:

        async def one_host(host: str) -> None:
            nonlocal done
            async with sem:
                catalogue = await read_brand_catalogue(client, host, attempts=args.attempts)
                seeds = by_host[host]
                delisted: List[Dict[str, Any]] = []
                if catalogue.usable:
                    for seed in seeds:
                        handle = (extract_product_handle(destination_of(seed)) or "").lower()
                        if handle not in catalogue.handles:
                            delisted.append(dict(seed))
                if args.probe:
                    for seed in delisted:
                        observation = await probe_destination(
                            client, destination_of(seed), listed_in_catalogue=False
                        )
                        seed["verdict"] = observation.verdict
                        seed["verdict_note"] = observation.note
                        seed["http_status"] = observation.http_status
                results[host] = {
                    "status": catalogue.status,
                    "note": catalogue.note,
                    "catalogue_products": catalogue.product_count,
                    "catalogue_handles": len(catalogue.handles),
                    "seeds": len(seeds),
                    "delisted": len(delisted),
                    "delisted_rows": delisted,
                }
                done += 1
                print(
                    f"[{done}/{len(hosts)}] {host}: {catalogue.status} "
                    f"catalogue={len(catalogue.handles)} seeds={len(seeds)} "
                    f"delisted={len(delisted)} {catalogue.note}",
                    flush=True,
                )

        await asyncio.gather(*(one_host(h) for h in hosts))

    return summarize(results, probed=args.probe)


def summarize(results: Dict[str, Dict[str, Any]], *, probed: bool) -> Dict[str, Any]:
    readable = {h: r for h, r in results.items() if r["status"] == CATALOGUE_OK}
    denominator = sum(r["seeds"] for r in readable.values())
    delisted = sum(r["delisted"] for r in readable.values())

    coverage: Counter = Counter()
    for r in results.values():
        coverage[r["status"]] += r["seeds"]

    verdicts: Counter = Counter()
    for r in readable.values():
        for seed in r["delisted_rows"]:
            verdicts[seed.get("verdict") or "not_probed"] += 1
    broken = sum(verdicts[v] for v in CONFIRMED_DEAD_VERDICTS)

    print("\n" + "=" * 72)
    print("COVERAGE — seeds per catalogue-read outcome (only 'ok' can be measured)")
    for status, count in coverage.most_common():
        print(f"  {status:20s} {count:6d}")
    print(
        f"\nDELISTED RATE over readable hosts: {delisted}/{denominator} = "
        f"{(100.0 * delisted / denominator) if denominator else 0:.2f}%"
    )
    if probed:
        print("\nWHAT A SHOPPER GETS on those delisted links:")
        for verdict in ALL_VERDICTS + ("not_probed",):
            count = verdicts.get(verdict, 0)
            if not count:
                continue
            share = 100.0 * count / delisted if delisted else 0
            flag = "  <- broken" if verdict in CONFIRMED_DEAD_VERDICTS else ""
            print(f"  {verdict:24s} {count:5d}  ({share:5.1f}% of delisted){flag}")
        print(
            f"\nCONFIRMED BROKEN: {broken}/{denominator} = "
            f"{(100.0 * broken / denominator) if denominator else 0:.2f}% of measured seeds"
        )

    return {
        "coverage_seeds_by_status": dict(coverage),
        "readable_hosts": len(readable),
        "measured_seeds": denominator,
        "delisted": delisted,
        "confirmed_broken": broken,
        "verdicts": dict(verdicts),
        "hosts": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seeds-file", default=None, help="JSON dump of seed rows (skips the DB)")
    parser.add_argument("--host", action="append", default=[], help="Limit to this host (repeatable)")
    parser.add_argument("--out", default=None, help="Write the full JSON report here")
    parser.add_argument("--host-concurrency", type=int, default=4)
    parser.add_argument("--attempts", type=int, default=3, help="Retries per catalogue page")
    parser.add_argument("--timeout", type=float, default=25.0)
    parser.add_argument(
        "--no-probe",
        dest="probe",
        action="store_false",
        help="Skip stage 2. The delisted count alone OVERSTATES dead links — see the docstring.",
    )
    parser.set_defaults(probe=True)
    args = parser.parse_args()

    report = asyncio.run(run(args))
    if args.out:
        with open(args.out, "w") as handle:
            json.dump(report, handle, indent=2, default=str)
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
