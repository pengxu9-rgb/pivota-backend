#!/usr/bin/env python3
"""DETECTIVE: which active external seeds point at a PDP the brand no longer sells?

`external_product_seeds` stores a third-party product URL that we then publish, without
ever re-reading it. Two lanes hand that URL to an agent or a shopper:

  * `offers.resolve` -> `affiliate_url` / `execution_spec.pdp_url`
    (routes/agent_shop_gateway._append_external_offers_from_seed_rows)
  * the canonical product read -> `merchant_canonical_url`
    (routes/pivota_canonical_routes, fed by the seed -> catalog_products mirror)

NOTHING IN THE CODEBASE EVER ASKS WHETHER THAT URL STILL RESOLVES. `stale_snapshot`
(services/external_referral_readiness) is a clock, not an observation, and it is fail-open
on a missing timestamp. `non_product_fallback_page` (services/external_seed_audit) is a
regex over the STORED title/description — it never fetches. `_refresh_external_seed_by_id`
does fetch, but `resolve_external_offer` calls `raise_for_status()`, so a 404 arrives as an
exception, is recorded as `{"status": "degraded"}`, and the seed stays `active` forever.
A dead link therefore has no way to become known.

WHAT THIS SCRIPT MEASURES, in two stages, because the cheap stage is not the truth:

  stage 1  per HOST, read the brand's own Shopify catalogue (`/products.json`) and join the
           seed handles against it. This is one request per 250 products and it finds every
           DELISTED handle at once. It is NOT proof of a dead link: a delisted handle can
           still render (measured: cosrx.com serves 5 of 12 delisted PDPs at 200).
  stage 2  probe each DELISTED PDP itself and classify what a shopper would actually get:
           dead_404 / redirected_off_product / redirected_to_product / live_delisted.

A HOST WE CANNOT READ IS NOT A HOST WITH DEAD LINKS. Two failure shapes are reported as
their own outcomes and are excluded from every rate, rather than being folded into "dead":

  * `bot_challenge` — Cloudflare answers 429 with `cf-mitigated: challenge` for EVERY path
    including robots.txt. Measured 2026-08-25 from a non-crawl-egress host: 213 of 286 seed
    hosts. Retrying or backing off cannot help; the client is being refused, not paced.
  * `incomplete` — pagination broke partway. The first version of this audit returned the
    partial catalogue as if it were the whole one, and reported 285 fabricated dead handles
    on fentybeauty.com alone. Every unread page is a false positive, so a truncated read is
    discarded entirely.

Read-only: it never writes to the database and has no `--apply`. Every outbound request
goes through `services.crawl_politeness` (robots + per-domain pacing + 429 backoff).

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
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import httpx

from services import crawl_politeness
from services.outbound_warm_handoff import _host_of, extract_product_handle

USER_AGENT = (
    "PivotaAuditBot/1.0 (+https://pivota.cc/about/audit-bot; "
    "checks that published product links still resolve)"
)
PAGE_LIMIT = 250
MAX_PAGES = 80


# --------------------------------------------------------------------------- stage 1

async def _get_catalogue_page(
    client: httpx.AsyncClient, url: str, attempts: int
) -> Tuple[str, Any]:
    """One `/products.json` page. Returns (kind, payload)."""
    last = ""
    for attempt in range(attempts):
        try:
            # max_wait=0 is UNBOUNDED and is the right choice for a batch: the default 10s
            # ceiling makes the backoff curve above ~16s unreachable, and `CrawlPaced` would
            # then be recorded as if the host had refused us.
            await crawl_politeness.before_request(url, user_agent=USER_AGENT, max_wait=0)
        except crawl_politeness.RobotsDisallowed:
            return "robots_disallowed", "robots.txt"
        except Exception as exc:  # noqa: BLE001
            return "gate_error", f"{type(exc).__name__}: {exc}"

        try:
            resp = await client.get(url, headers={"User-Agent": USER_AGENT})
        except Exception as exc:  # noqa: BLE001
            last = type(exc).__name__
            await asyncio.sleep(2 * (attempt + 1))
            continue

        crawl_politeness.note_response(
            url, resp.status_code, retry_after=resp.headers.get("retry-after")
        )
        # A bot challenge is served as 429 but is NOT a pacing signal — see the module
        # docstring. Returning immediately keeps a whole run from stalling on it.
        mitigated = resp.headers.get("cf-mitigated")
        if mitigated:
            return "bot_challenge", f"cf-mitigated={mitigated} http_{resp.status_code}"
        if resp.status_code == 429 or resp.status_code >= 500:
            last = f"http_{resp.status_code}"
            await asyncio.sleep(3 * (attempt + 1))
            continue
        if resp.status_code != 200:
            return "http", f"http_{resp.status_code}"
        if "json" not in (resp.headers.get("content-type") or "").lower():
            return "not_json", (resp.headers.get("content-type") or "")[:60]
        try:
            return "products", (resp.json() or {}).get("products") or []
        except Exception:  # noqa: BLE001
            return "bad_json", ""
    return "exhausted", last or "retries exhausted"


async def read_catalogue(
    client: httpx.AsyncClient, host: str, attempts: int
) -> Tuple[str, Set[str], int, str]:
    """Return (status, handles, product_count, note). Only status 'ok' is a catalogue."""
    handles: Set[str] = set()
    total = 0
    for page in range(1, MAX_PAGES + 1):
        url = f"https://{host}/products.json?limit={PAGE_LIMIT}&page={page}"
        kind, payload = await _get_catalogue_page(client, url, attempts)
        if kind != "products":
            note = payload if isinstance(payload, str) else str(payload)
            if page == 1:
                return (note if kind == "http" else kind), handles, total, note
            # Partial pagination is discarded, not reported — see the module docstring.
            return "incomplete", set(), total, f"broke at page {page}: {note}"
        if not payload:
            return "ok", handles, total, f"{page - 1} page(s)"
        total += len(payload)
        for product in payload:
            handle = str((product or {}).get("handle") or "").strip().lower()
            if handle:
                handles.add(handle)
        if len(payload) < PAGE_LIMIT:
            return "ok", handles, total, f"{page} page(s)"
    return "incomplete", set(), total, f"hit MAX_PAGES={MAX_PAGES}"


# --------------------------------------------------------------------------- stage 2

async def probe_pdp(client: httpx.AsyncClient, url: str) -> Tuple[str, str]:
    """What would a shopper following this link actually get?"""
    try:
        await crawl_politeness.before_request(url, user_agent=USER_AGENT, max_wait=0)
    except crawl_politeness.RobotsDisallowed:
        return "unverifiable", "robots.txt"
    except Exception as exc:  # noqa: BLE001
        return "unverifiable", type(exc).__name__

    try:
        resp = await client.get(url, headers={"User-Agent": USER_AGENT})
    except Exception as exc:  # noqa: BLE001
        return "unverifiable", type(exc).__name__

    crawl_politeness.note_response(
        url, resp.status_code, retry_after=resp.headers.get("retry-after")
    )
    if resp.headers.get("cf-mitigated"):
        return "unverifiable", "bot_challenge"
    if resp.status_code in (404, 410):
        return "dead_404", f"http_{resp.status_code}"
    if resp.status_code >= 400:
        return "unverifiable", f"http_{resp.status_code}"

    final = str(resp.url)
    want = (extract_product_handle(url) or "").lower()
    got = (extract_product_handle(final) or "").lower()
    if not got:
        # 301 to a collection or the homepage. The shopper does not see a 404, but the
        # product they were shown is not on the page they land on.
        return "redirected_off_product", final[:200]
    if got != want:
        return "redirected_to_product", final[:200]
    # Still the same handle at 200: published to the storefront, absent from the JSON
    # catalogue. The link works; the row is invisible to every catalogue-derived join.
    return "live_delisted", ""


# --------------------------------------------------------------------------- corpus

async def load_seeds_from_db() -> List[Dict[str, Any]]:
    from db.database import database  # imported late: the file-mode path needs no DB

    await database.connect()
    try:
        rows = await database.fetch_all(
            """
            SELECT id, status, domain, canonical_url, destination_url,
                   created_at, updated_at, attached_product_key
            FROM external_product_seeds
            WHERE status = 'active'
            """,
            {},
        )
    finally:
        await database.disconnect()
    return [dict(row) for row in rows or []]


def group_by_host(
    rows: Sequence[Dict[str, Any]], only_hosts: Optional[Set[str]]
) -> Dict[str, List[Dict[str, Any]]]:
    by_host: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        dest = str(row.get("canonical_url") or row.get("destination_url") or "")
        handle = extract_product_handle(dest)
        if not handle:
            continue
        # A LOCALE STOREFRONT IS ITS OWN CATALOGUE. `nl.beautyofjoseon.com` and
        # `beautyofjoseon.com` do not list the same handles, so the host is taken verbatim
        # off the destination rather than folded to an apex.
        host = _host_of(dest)
        if only_hosts and host not in only_hosts:
            continue
        by_host[host].append(
            {
                "id": row.get("id"),
                "handle": handle.lower(),
                "url": dest,
                "updated_at": str(row.get("updated_at")),
                "attached": bool(row.get("attached_product_key")),
            }
        )
    return by_host


# --------------------------------------------------------------------------- run

async def run(args: argparse.Namespace) -> Dict[str, Any]:
    if args.seeds_file:
        rows = json.load(open(args.seeds_file))
    else:
        rows = await load_seeds_from_db()

    by_host = group_by_host(rows, set(args.host) if args.host else None)
    hosts = sorted(by_host, key=lambda h: -len(by_host[h]))
    handle_bearing = sum(len(v) for v in by_host.values())
    print(
        f"{len(rows)} active seeds -> {handle_bearing} with a /products/<handle> "
        f"across {len(hosts)} hosts",
        flush=True,
    )

    host_sem = asyncio.Semaphore(args.host_concurrency)
    results: Dict[str, Dict[str, Any]] = {}
    done = 0

    async with httpx.AsyncClient(timeout=args.timeout, follow_redirects=True) as client:

        async def one_host(host: str) -> None:
            nonlocal done
            async with host_sem:
                status, handles, total, note = await read_catalogue(client, host, args.attempts)
                seeds = by_host[host]
                delisted = (
                    [s for s in seeds if s["handle"] not in handles] if status == "ok" else []
                )
                if args.probe and delisted:
                    for seed in delisted:
                        seed["verdict"], seed["verdict_note"] = await probe_pdp(
                            client, seed["url"]
                        )
                results[host] = {
                    "status": status,
                    "note": note,
                    "catalogue_products": total,
                    "catalogue_handles": len(handles),
                    "seeds": len(seeds),
                    "delisted": len(delisted),
                    "delisted_rows": delisted,
                }
                done += 1
                print(
                    f"[{done}/{len(hosts)}] {host}: {status} catalogue={len(handles)} "
                    f"seeds={len(seeds)} delisted={len(delisted)} {note}",
                    flush=True,
                )

        await asyncio.gather(*(one_host(h) for h in hosts))

    return summarize(results, probed=args.probe)


def summarize(results: Dict[str, Dict[str, Any]], *, probed: bool) -> Dict[str, Any]:
    readable = {h: r for h, r in results.items() if r["status"] == "ok"}
    denominator = sum(r["seeds"] for r in readable.values())
    delisted = sum(r["delisted"] for r in readable.values())

    coverage = Counter()
    for r in results.values():
        coverage[r["status"]] += r["seeds"]

    verdicts = Counter()
    for r in readable.values():
        for seed in r["delisted_rows"]:
            verdicts[seed.get("verdict") or "not_probed"] += 1

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
        for verdict, count in verdicts.most_common():
            share = 100.0 * count / delisted if delisted else 0
            print(f"  {verdict:24s} {count:5d}  ({share:5.1f}% of delisted)")

    return {
        "coverage_seeds_by_status": dict(coverage),
        "readable_hosts": len(readable),
        "measured_seeds": denominator,
        "delisted": delisted,
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
