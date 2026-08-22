#!/usr/bin/env python3
"""Backfill NUMERIC Shopify variant ids onto crawled seeds, from each storefront's `.js`.

Dry-run is the default. Pass --apply to write.

WHY. `shopify_cart_base_url` refuses to fabricate a variant id, so a pre-filled cart URL —
the thing that turns a cold redirect into a handoff an agent can complete — is unbuildable
without one. Measured 2026-08-21 on a 3,000-row sample of the serving corpus: 28.0% of rows
carry a variant with any id; 42% of the live products are genuinely multi-variant. A live
probe recovered numeric ids for 81 of 81 reachable Shopify PDPs, so the data is available
and simply was never collected.

The decision logic lives in `services/shopify_variant_identity` and is unit-tested without
a network or a database. This file is the I/O: selection, pacing, writes, and the report.

THIS IS A ONE-SHOT OPS SCRIPT, NOT A SCHEDULED JOB, AND THAT IS DELIBERATE.
Every Cloud Run service and job egresses through ONE reserved Cloud NAT address
(infra/gcp/setup_egress_nat.sh uses --nat-all-subnet-ip-ranges with a single IP). On prod
that address is 8.231.167.230 — the same one being given to Antom/Adyen for payment
allowlisting, reserved precisely so it never changes. Crawl traffic on it shares both IP
reputation and the NAT port pool with the payment path, on an address that cannot be rotated
without a partner re-allowlisting cycle. Until crawl egress has its own subnet + router +
reserved IP, this runs from an operator machine. Do not add it to
`infra/gcp/setup_scheduler.sh`.

PACING IS NOT POLITENESS THEATRE — IT IS MEASURED. On 2026-08-21, ~50 requests spread over
37 Cloudflare-fronted domains in about one minute tripped a CROSS-DOMAIN, IP-level 429 that
persisted ~15 minutes, including on domains that had answered 200 moments earlier. The
cohort is ~100% Cloudflare-fronted. So: one request per second globally, a minimum gap per
domain, and a circuit breaker that stops the run rather than spending an hour being
throttled and calling the result "coverage".

USAGE
    python -m scripts.backfill_shopify_variant_ids --limit 200            # dry run
    python -m scripts.backfill_shopify_variant_ids --limit 200 --apply
    python -m scripts.backfill_shopify_variant_ids --domain genabelle.com --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db.database import database  # noqa: E402
from services.shopify_variant_identity import (  # noqa: E402
    apply_variant_ids,
    parse_product_js,
    product_js_url,
)

# Matches services/external_offers_service.DEFAULT_UA: a probe that identifies itself the
# same way the crawler does keeps "does this domain block us" answerable from one signal.
USER_AGENT = os.getenv("EXTERNAL_OFFER_USER_AGENT") or "Mozilla/5.0 (compatible; PivotaBot/1.0; +https://pivota.cc)"

GLOBAL_MIN_INTERVAL_S = float(os.getenv("VARIANT_BACKFILL_GLOBAL_INTERVAL_S", "1.0"))
PER_DOMAIN_MIN_GAP_S = float(os.getenv("VARIANT_BACKFILL_DOMAIN_GAP_S", "3.0"))
REQUEST_TIMEOUT_S = float(os.getenv("VARIANT_BACKFILL_TIMEOUT_S", "20"))
# Stop the run rather than spend an hour throttled. A sustained 429 streak means the IP is
# blocked, and every further request deepens it while collecting nothing.
CONSECUTIVE_429_ABORT = int(os.getenv("VARIANT_BACKFILL_ABORT_AFTER_429", "8"))


class Pacer:
    """One global spigot plus a per-domain floor."""

    def __init__(self) -> None:
        self._last_global = 0.0
        self._last_by_domain: Dict[str, float] = defaultdict(float)

    async def wait(self, domain: str) -> None:
        now = time.monotonic()
        delay = max(
            GLOBAL_MIN_INTERVAL_S - (now - self._last_global),
            PER_DOMAIN_MIN_GAP_S - (now - self._last_by_domain[domain]),
            0.0,
        )
        if delay > 0:
            await asyncio.sleep(delay)
        stamp = time.monotonic()
        self._last_global = stamp
        self._last_by_domain[domain] = stamp


async def select_candidates(limit: int, domain: Optional[str]) -> List[Dict[str, Any]]:
    """Active seeds that could gain a numeric variant id.

    Ordered oldest-touched first so a partial run makes progress on the stalest rows rather
    than re-walking the same head every time.
    """
    clauses = [
        "status = 'active'",
        "COALESCE(canonical_url, destination_url) IS NOT NULL",
        "COALESCE(canonical_url, destination_url) <> ''",
    ]
    values: Dict[str, Any] = {"limit": max(1, int(limit))}
    if domain:
        clauses.append("domain = :domain")
        values["domain"] = domain
    rows = await database.fetch_all(
        f"""
        SELECT id, domain, canonical_url, destination_url, seed_data
        FROM external_product_seeds
        WHERE {' AND '.join(clauses)}
        ORDER BY updated_at ASC NULLS FIRST, created_at ASC NULLS FIRST
        LIMIT :limit
        """,
        values,
    )
    return [dict(r) for r in rows or []]


def _seed_data_of(row: Dict[str, Any]) -> Dict[str, Any]:
    raw = row.get("seed_data")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def already_covered(seed_data: Dict[str, Any]) -> bool:
    variants = seed_data.get("variants")
    if not isinstance(variants, list) or not variants:
        return False
    return all(
        isinstance(v, dict) and str(v.get("shopify_variant_id") or "").strip()
        for v in variants
    )


async def fetch_product_js(client: httpx.AsyncClient, url: str) -> tuple[Optional[Any], str]:
    """Return (payload, outcome). Never raises — an unreachable page is data, not an error."""
    try:
        resp = await client.get(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=REQUEST_TIMEOUT_S,
            follow_redirects=True,
        )
    except Exception as exc:  # noqa: BLE001 - outcome is classified, not swallowed
        return None, f"error:{type(exc).__name__}"
    if resp.status_code == 429:
        return None, "rate_limited"
    if resp.status_code == 404:
        # A 404 here is a real signal: the handle is gone. Worth counting separately from a
        # block, because it means the seed's URL is dead — 6.7% of a live sample was.
        return None, "dead_handle"
    if resp.status_code >= 400:
        return None, f"http_{resp.status_code}"
    ctype = (resp.headers.get("content-type") or "").lower()
    if "json" not in ctype and "javascript" not in ctype:
        # A themed HTML 404 served with status 200 is the common soft-404 shape.
        return None, "not_json"
    try:
        return resp.json(), "ok"
    except Exception:  # noqa: BLE001
        return None, "unparseable"


async def run(limit: int, domain: Optional[str], apply: bool) -> Dict[str, Any]:
    rows = await select_candidates(limit=limit, domain=domain)
    pacer = Pacer()
    outcomes: Counter = Counter()
    reasons: Counter = Counter()
    per_domain_failures: Counter = Counter()
    stamped_total = 0
    changed_rows = 0
    consecutive_429 = 0
    aborted = False

    async with httpx.AsyncClient() as client:
        for row in rows:
            seed_data = _seed_data_of(row)
            if already_covered(seed_data):
                outcomes["already_covered"] += 1
                continue

            page_url = row.get("canonical_url") or row.get("destination_url")
            js_url = product_js_url(page_url)
            if not js_url:
                outcomes["not_a_shopify_product_url"] += 1
                continue

            host = urlparse(js_url).hostname or (row.get("domain") or "unknown")
            await pacer.wait(host)
            payload, outcome = await fetch_product_js(client, js_url)
            outcomes[outcome] += 1

            if outcome == "rate_limited":
                consecutive_429 += 1
                per_domain_failures[host] += 1
                if consecutive_429 >= CONSECUTIVE_429_ABORT:
                    aborted = True
                    break
                continue
            consecutive_429 = 0
            if outcome != "ok":
                per_domain_failures[host] += 1
                continue

            live = parse_product_js(payload)
            new_seed_data, report = apply_variant_ids(seed_data, live)
            reasons[report["reason"]] += 1
            if report["stamped"] <= 0:
                continue

            stamped_total += report["stamped"]
            changed_rows += 1
            if apply:
                await database.execute(
                    """
                    UPDATE external_product_seeds
                    SET seed_data = CAST(:seed_data AS JSONB), updated_at = NOW()
                    WHERE id = :id
                    """,
                    {"id": row["id"], "seed_data": json.dumps(new_seed_data, ensure_ascii=False)},
                )

    return {
        "mode": "apply" if apply else "dry_run",
        "aborted_on_rate_limit": aborted,
        "candidates": len(rows),
        "rows_with_new_ids": changed_rows,
        "variant_ids_stamped": stamped_total,
        "fetch_outcomes": dict(outcomes),
        "match_reasons": dict(reasons),
        "worst_domains": dict(per_domain_failures.most_common(10)),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--domain", type=str, default=None, help="restrict to one storefront")
    parser.add_argument("--apply", action="store_true", help="write; omit for a dry run")
    args = parser.parse_args()

    async def _main() -> Dict[str, Any]:
        await database.connect()
        try:
            return await run(limit=args.limit, domain=args.domain, apply=args.apply)
        finally:
            await database.disconnect()

    summary = asyncio.run(_main())
    print(json.dumps(summary, indent=2, ensure_ascii=False), flush=True)
    # A run that aborted on rate limiting collected partial data; exiting non-zero keeps a
    # wrapper from reading it as a completed sweep.
    return 1 if summary.get("aborted_on_rate_limit") else 0


if __name__ == "__main__":
    raise SystemExit(main())
