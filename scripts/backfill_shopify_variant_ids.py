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
    parse_product_js,
    product_js_url,
    stamp_variant_ids,
)

# Matches services/external_offers_service.DEFAULT_UA: a probe that identifies itself the
# same way the crawler does keeps "does this domain block us" answerable from one signal.
USER_AGENT = os.getenv("EXTERNAL_OFFER_USER_AGENT") or "Mozilla/5.0 (compatible; PivotaBot/1.0; +https://pivota.cc)"

GLOBAL_MIN_INTERVAL_S = float(os.getenv("VARIANT_BACKFILL_GLOBAL_INTERVAL_S", "1.0"))
PER_DOMAIN_MIN_GAP_S = float(os.getenv("VARIANT_BACKFILL_DOMAIN_GAP_S", "3.0"))
REQUEST_TIMEOUT_S = float(os.getenv("VARIANT_BACKFILL_TIMEOUT_S", "20"))
# Stop the run rather than spend an hour throttled. A sustained 429 streak means the IP is
# blocked, and every further request deepens it while collecting nothing.
CONSECUTIVE_BLOCK_ABORT = int(os.getenv("VARIANT_BACKFILL_ABORT_AFTER_BLOCKS", "8"))
# Every shape a bot block actually takes, not just the one observed on 2026-08-21.
BLOCK_OUTCOMES = frozenset({"rate_limited", "http_403", "http_503", "not_json"})


# Hoisted so its guards are assertable without a database. Review found that EVERY
# write-path guard survived mutation — including one whose reversion (writing to
# '{variants}' instead of '{snapshot,variants}') silently re-opened a blocker while the
# suite stayed green. A statement no test can see is a statement no test protects.
STAMP_UPDATE_SQL = """
                    UPDATE external_product_seeds
                    SET seed_data = jsonb_set(
                            seed_data,
                            '{snapshot,variants}',
                            CAST(:variants AS jsonb),
                            true
                        )
                    WHERE id = :id
                      AND jsonb_typeof(seed_data) = 'object'
                      AND updated_at IS NOT DISTINCT FROM :updated_at
                    RETURNING id
"""


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
    """Seeds that could gain a numeric variant id.

    EVERY eligibility condition is in SQL, before the LIMIT. An earlier version filtered
    "is it a Shopify product URL" and "is it already covered" in Python AFTERWARDS, so a run
    selected the oldest N rows, discarded most of them, and the next run re-selected the
    identical unproductive prefix — burning the 1 req/s budget forever without reaching row
    N+1. That is the trap scripts/run_seed_content_audit.py documents.

    `jsonb_typeof(seed_data) = 'object'` guards the double-encoded-string shape: asyncpg
    returns JSONB as a dict OR a JSON string depending on codec, and a row that arrives as a
    string must never be merged into. Same guard as scripts/backfill_crawl_seed_variants.py.

    Ordered by id, not updated_at: this script deliberately does not bump `updated_at`, so
    ordering by it would re-walk the same head every run.
    """
    clauses = [
        "status = 'active'",
        "jsonb_typeof(seed_data) = 'object'",
        # NULLIF, not COALESCE alone: `canonical_url = ''` is not NULL, so COALESCE kept
        # the empty string and the regex dropped the row even when destination_url was
        # perfectly good. The Python at the fetch site uses `or`, which DOES fall through
        # — the two halves disagreed and the fallback was unreachable.
        "COALESCE(NULLIF(canonical_url, ''), destination_url) ~ '/products/'",
        # THE TYPE GUARD MUST COME BEFORE THE ARRAY FUNCTIONS. `jsonb_array_length` and
        # `jsonb_array_elements` RAISE on a non-array, and `jsonb_typeof(seed_data) =
        # 'object'` constrains only the TOP level — so one row whose snapshot.variants is
        # an object or a scalar aborted the entire query with "cannot get array length of
        # a non-array", and the script produced a traceback instead of a cohort. Postgres
        # does not guarantee clause order, so this is a guard on the value's type, not a
        # bet on short-circuiting.
        "jsonb_typeof(seed_data->'snapshot'->'variants') = 'array'",
        "jsonb_array_length(seed_data->'snapshot'->'variants') > 0",
        # A content-locked snapshot is REFUSED, not bypassed. trg_enforce_seed_data_lock
        # (migration 081) logs a violation when a write changes a top-level seed_data key
        # named in content_lock, and this write changes `snapshot`. The trigger is
        # RAISE NOTICE today and documented to become RAISE EXCEPTION "once bypass volume
        # = 0" — so bypassing would both inflate the number gating that flip and hard-fail
        # afterwards. An id we cannot stamp is a row we skip.
        "NOT (COALESCE(content_lock, '{}'::jsonb) ? 'snapshot')",
        # At least one variant still missing the id.
        """EXISTS (
             SELECT 1 FROM jsonb_array_elements(seed_data->'snapshot'->'variants') v
             WHERE COALESCE(v->>'shopify_variant_id', '') = ''
           )""",
    ]
    values: Dict[str, Any] = {"limit": max(1, int(limit))}
    if domain:
        # Suffix match, not equality: a stored `www.brand.com` against `--domain brand.com`
        # reported `candidates: 0` and exited 0, which reads as "nothing to do".
        clauses.append("(domain = :domain OR domain LIKE :domain_suffix)")
        values["domain"] = domain
        values["domain_suffix"] = f"%.{domain}"
    rows = await database.fetch_all(
        f"""
        SELECT id, domain, canonical_url, destination_url, seed_data, updated_at
        FROM external_product_seeds
        WHERE {' AND '.join(clauses)}
        ORDER BY id
        LIMIT :limit
        """,
        values,
    )
    return [dict(r) for r in rows or []]


def _seed_data_of(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """None means UNREADABLE, and an unreadable row must be skipped, never written.

    Returning {} here was a document-destroying bug: the caller would then stamp onto an
    empty dict and write it as the WHOLE seed_data, erasing title, description, images,
    manual_overrides and snapshot. The SQL `jsonb_typeof` guard makes this near-unreachable;
    this is the second lock on the same door.
    """
    raw = row.get("seed_data")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            return None
    return None


def snapshot_variants(seed_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The crawl cohort keeps its variants HERE, not at the top level.

    Top-level `variants` is what the serving readers prefer
    (`beauty_external_ranking._normalize_seed_variants`, `agent_api._seed_variants`), so
    writing there would shadow the real array with whatever this script produced. Stamping
    in place inside snapshot leaves that precedence exactly as it was.
    """
    snapshot = seed_data.get("snapshot")
    if not isinstance(snapshot, dict):
        return []
    variants = snapshot.get("variants")
    if not isinstance(variants, list):
        return []
    return [v for v in variants if isinstance(v, dict)]


def already_covered(seed_data: Dict[str, Any]) -> bool:
    variants = snapshot_variants(seed_data)
    if not variants:
        return False
    return all(str(v.get("shopify_variant_id") or "").strip() for v in variants)


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
    conflicts = 0
    consecutive_blocks = 0
    aborted = False

    async with httpx.AsyncClient() as client:
        for row in rows:
            seed_data = _seed_data_of(row)
            if seed_data is None:
                outcomes["unreadable_seed_data"] += 1
                continue
            raw_variants = (seed_data.get("snapshot") or {}).get("variants")
            variants = snapshot_variants(seed_data)
            if not variants:
                outcomes["no_snapshot_variants"] += 1
                continue
            if not isinstance(raw_variants, list) or len(raw_variants) != len(variants):
                # `snapshot_variants` filters non-dict elements, and the filtered list
                # becomes the ENTIRE new array via jsonb_set — so a scalar sitting in the
                # array would be silently deleted by a write that claims only to stamp an
                # id. Same class as the document-destruction blocker, one level down.
                outcomes["malformed_variant_element"] += 1
                continue
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

            # THE BREAKER COUNTS BLOCKS, NOT ONLY LITERAL 429s. A Cloudflare bot block is
            # just as often a 403, or a 200 serving challenge HTML (-> not_json). Counting
            # only 429 meant a domain 403ing every row reset the streak forever and the run
            # ground on, blocked, calling the result coverage.
            if outcome in BLOCK_OUTCOMES:
                consecutive_blocks += 1
                per_domain_failures[host] += 1
                if consecutive_blocks >= CONSECUTIVE_BLOCK_ABORT:
                    aborted = True
                    break
                continue
            consecutive_blocks = 0
            if outcome != "ok":
                per_domain_failures[host] += 1
                continue

            live = parse_product_js(payload)
            new_variants, report = stamp_variant_ids(variants, live)
            reasons[report["reason"]] += 1
            if report["stamped"] <= 0:
                continue

            stamped_total += report["stamped"]
            changed_rows += 1
            if apply:
                # A JSONB MERGE ON ONE KEY, not a whole-document replace, and guarded on the
                # updated_at we read. The refresh job (routes/employee_products.
                # _refresh_external_seed_by_id) rewrites this same `snapshot.variants` key,
                # and this loop runs for minutes at 1 req/s — a stale whole-document write
                # would silently revert it. Same shape as
                # scripts/backfill_agent_seed_variants.sql.
                #
                # `updated_at` is deliberately NOT bumped: a variants-only .js fetch is not
                # an extraction event, and `stale_snapshot` (7d) falls back to updated_at, so
                # bumping it would clear a runtime BLOCKER on evidence we did not collect.
                # scripts/backfill_crawl_seed_variants.py holds the same line.
                # RETURNING id, NOT bare execute(). `databases` 0.7.0 implements
                # execute() as `connection.fetchval(...)`, which returns None for a
                # non-RETURNING UPDATE regardless of rows affected — so `if not result`
                # fired on EVERY write. --apply reported `stamped: 0` and counted every
                # successful write as a conflict while the writes actually landed, which
                # reads as total failure to the operator and makes a real conflict
                # indistinguishable from a success. The prior-art script this comment
                # cites avoids it by reading the command tag off a raw asyncpg
                # connection; RETURNING is the equivalent through this driver.
                written_id = await database.fetch_val(
                    STAMP_UPDATE_SQL,
                    {
                        "id": row["id"],
                        "variants": json.dumps(new_variants, ensure_ascii=False, default=str),
                        "updated_at": row.get("updated_at"),
                    },
                )
                if written_id is None:
                    # Someone else wrote the row between our read and our write. Their data
                    # is newer; counted, never forced.
                    conflicts += 1
                    changed_rows -= 1
                    stamped_total -= report["stamped"]

    return {
        "mode": "apply" if apply else "dry_run",
        "aborted_on_rate_limit": aborted,
        "candidates": len(rows),
        "rows_with_new_ids": changed_rows,
        "variant_ids_stamped": stamped_total,
        "write_conflicts": conflicts,
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
