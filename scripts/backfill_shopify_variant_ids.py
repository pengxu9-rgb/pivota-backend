#!/usr/bin/env python3
"""Backfill Shopify storefront evidence onto crawled seeds, from each storefront's `.js`.

Dry-run is the default. Pass --apply to write.

WHAT THIS PRODUCES, AND WHO CONSUMES IT. #1813 shipped the consumer first, deliberately:
`routes/agent_shop_gateway._external_seed_redirect_identity` now flips a crawl seed's
intake-lane label to a real `platform="shopify"` and prefills a cart permalink — but only
on explicit stored evidence, which nothing writes yet. This is that writer. Per seed it
stamps into `seed_data.snapshot`:

    storefront_platform        = "shopify"          <- proves the is_shopify gate's predicate
    storefront_platform_source = "products_js_v1"   <- provenance, so the claim is auditable
    variants[].shopify_variant_id                   <- the numeric id the permalink needs

A successful `/products/<handle>.js` parse IS the proof of Shopify-ness: only Shopify serves
that endpoint in that shape. So the same fetch that recovers variant ids also establishes
the platform, and both are written together or not at all.

Measured 2026-08-21 on a 3,000-row sample of the serving corpus: 28.0% of rows carry a
variant with any id, while 42% of live products are multi-variant. A live probe recovered
numeric ids for 81 of 81 reachable Shopify PDPs, and the resulting permalink was verified
end to end into a real Shopify checkout page (see #1813).

WHERE IT WRITES. `seed_data.snapshot.variants` — the array the crawl cohort actually
authors (scripts/onboard_external_brand_from_crawl.py) and the one both consumer helpers
read. NOT top-level `variants`, which would shadow it for the serving readers.

DECISION LOGIC LIVES IN services/shopify_variant_identity AND IS TESTED WITHOUT I/O.
This file is selection, pacing, writes and the report. The SQL is exercised against a real
Postgres by tests/test_backfill_shopify_variant_ids_postgres.py, because three of this
script's four historical P0s were SQL semantics no Python-level test could see.

ONE-SHOT OPS SCRIPT, NOT A SCHEDULED JOB, DELIBERATELY. Every Cloud Run service and job
egresses through ONE reserved Cloud NAT address (infra/gcp/setup_egress_nat.sh uses
--nat-all-subnet-ip-ranges with a single IP); on prod that is 8.231.167.230, the address
given to Antom/Adyen for payment allowlisting. Crawl traffic there shares IP reputation and
the NAT port pool with the payment path, on an address that cannot be rotated without a
partner re-allowlisting cycle. Until crawl egress has its own subnet, this runs from an
operator machine. Do not add it to infra/gcp/setup_scheduler.sh.

PACING IS MEASURED, NOT DECORATIVE. On 2026-08-21, ~50 requests over 37 Cloudflare-fronted
domains in about a minute tripped a CROSS-DOMAIN, IP-level 429 lasting ~15 minutes,
including on domains that had answered 200 moments earlier. The cohort is ~100%
Cloudflare-fronted.

KNOWN INTERACTION, NOT HANDLED HERE: re-running scripts/onboard_external_brand_from_crawl.py
upserts `seed_data = COALESCE(...) || EXCLUDED.seed_data`, a SHALLOW merge that replaces the
whole `snapshot` object and therefore wipes everything this script stamps. Re-run this after
any onboarding pass over the same brand. Fixing that merge is onboarding's change to make.

USAGE
    python -m scripts.backfill_shopify_variant_ids --limit 200            # dry run
    python -m scripts.backfill_shopify_variant_ids --domain genabelle.com --limit 5 --apply
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
from typing import Any, Dict, List, Optional, Tuple
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
CONSECUTIVE_BLOCK_ABORT = int(os.getenv("VARIANT_BACKFILL_ABORT_AFTER_BLOCKS", "8"))
# Every shape a bot block actually takes, not only the 429 observed on 2026-08-21. A
# Cloudflare block is commonly 403, or 200 + a challenge page.
#
# `not_json` is NOT here, and that is a correction: it was, and it conflates a Cloudflare
# challenge with a THEMED SOFT-404 — a dead product handle rendering the shop's 404 page with
# status 200. Those are the same bytes to us but opposite facts, and treating a brand's dead
# handles as a block aborted the entire sweep on one brand's rot. A challenge page is
# indistinguishable here, so it is counted as a per-domain signal (`most_blocked_domains`)
# rather than allowed to stop the run.
#
# `error:*` IS here, via the prefix check below: an IP-level block usually presents as 403s
# INTERLEAVED with connection resets, and treating a reset as "not a block" reset the counter
# on every other request — 40 requests into a live block without aborting, measured.
BLOCK_OUTCOMES = frozenset({"rate_limited", "http_403", "http_503", "http_502", "http_520", "http_521"})


def _is_block(outcome: str) -> bool:
    return outcome in BLOCK_OUTCOMES or outcome.startswith("error:")

STOREFRONT_PLATFORM = "shopify"
STOREFRONT_PLATFORM_SOURCE = "products_js_v1"

# --------------------------------------------------------------------------------------
# SQL. Hoisted so the statements are assertable AND executable by the Postgres gate.
#
# EVERY jsonb ACCESSOR THAT CAN RAISE IS CASE-WRAPPED. `jsonb_array_length` and
# `jsonb_array_elements` raise on a non-array, and a sibling `jsonb_typeof(...) = 'array'`
# qual does NOT protect them: Postgres reorders quals freely, and an EXPLAIN on this very
# query put the array function FIRST. A single malformed row then aborted the whole run with
# "cannot get array length of a non-array". A CASE wrap is evaluated as one expression, so
# the guard cannot be separated from the thing it guards. This is the pattern the repo
# already uses at routes/agent_shop_gateway.py:4027.
_SNAPSHOT_VARIANTS_SAFE = (
    "CASE WHEN jsonb_typeof(seed_data->'snapshot'->'variants') = 'array' "
    "THEN seed_data->'snapshot'->'variants' ELSE '[]'::jsonb END"
)

SELECT_CANDIDATES_SQL = f"""
    SELECT id, domain, canonical_url, destination_url, seed_data, updated_at
    FROM external_product_seeds
    WHERE status = 'active'
      -- asyncpg hands JSONB back as a dict OR a JSON string depending on codec; a row that
      -- arrives as a string must never be merged into (it would be replaced wholesale).
      AND jsonb_typeof(seed_data) = 'object'
      -- NULLIF, not COALESCE alone: canonical_url = '' is not NULL, so COALESCE kept the
      -- empty string and dropped rows whose destination_url was perfectly good.
      AND COALESCE(NULLIF(canonical_url, ''), destination_url) ~ '/products/'
      AND jsonb_array_length({_SNAPSHOT_VARIANTS_SAFE}) > 0
      -- Not already covered: at least one variant still lacks a stamped id. Keeps a
      -- partially-covered row eligible, and retires it once every variant is stamped.
      AND EXISTS (
            SELECT 1 FROM jsonb_array_elements({_SNAPSHOT_VARIANTS_SAFE}) AS v
            -- NUMERIC, not merely non-empty. A live writer lands unvalidated variant keys
            -- here (recover_seed_data_from_catalog_extract -> seed_data_writer), and junk like
            -- `true` would otherwise mark the row covered and retire it from the backfill
            -- permanently — unrepairable, and useless to the consumer, which requires numeric.
            WHERE COALESCE(v->>'shopify_variant_id', '') !~ '^[0-9]+$'
          )
      {{domain_clause}}
      {{cursor_clause}}
    -- ORDER BY id + a CURSOR, because eligibility alone is not progress. A row that can never
    -- be stamped — a dead handle (6.7% of a live sample), or any `no_confident_match` — stays
    -- eligible forever and sorts to the front, so a plain `ORDER BY id LIMIT n` re-fetched the
    -- identical unproductive prefix on every run and never reached row n+1. Measured: four
    -- consecutive runs, same three dead rows, zero stamped. `--after` pages past them; the run
    -- reports `next_cursor` so a sweep can be resumed rather than restarted.
    ORDER BY id
    LIMIT :limit
"""

# jsonb_set on two paths in one statement: the variants array and the platform evidence.
# `updated_at` is deliberately NOT bumped — get_last_extracted_at falls back to it, feeding
# the 7-day stale_snapshot BLOCKER, and a variants-only .js fetch is not an extraction
# event. The optimistic guard makes this safe against the refresh job, which rewrites the
# same document. RETURNING because databases.execute() is fetchval and yields None for a
# non-RETURNING UPDATE either way — without it every successful write read as a conflict.
STAMP_UPDATE_SQL = """
    UPDATE external_product_seeds
    SET seed_data = jsonb_set(
            jsonb_set(
                jsonb_set(seed_data, '{snapshot,variants}', CAST(:variants AS jsonb), true),
                '{snapshot,storefront_platform}', to_jsonb(CAST(:platform AS text)), true
            ),
            '{snapshot,storefront_platform_source}', to_jsonb(CAST(:platform_source AS text)), true
        )
    WHERE id = :id
      -- THE LOAD-BEARING GUARD IS THE SNAPSHOT ONE (mutation-verified: dropping it is the
      -- only one of these that turns a refusal into a raise). `jsonb_set` on a scalar raises
      -- "cannot set path in scalar", which would abort a sweep mid-run instead of skipping
      -- one row. The seed_data typeof check below is redundant given it — `->` on a scalar
      -- yields NULL — and is kept only as a cheap statement of intent; do not mistake it for
      -- the protection.
      AND jsonb_typeof(seed_data->'snapshot') = 'object'
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


async def select_candidates(
    limit: int, domain: Optional[str], after: Optional[str] = None
) -> List[Dict[str, Any]]:
    values: Dict[str, Any] = {"limit": max(1, int(limit))}
    cursor_clause = ""
    if after:
        cursor_clause = "AND id > :after"
        values["after"] = after
    domain_clause = ""
    if domain:
        # Suffix match with a leading dot OR exact, so `--domain brand.com` covers
        # www.brand.com and shop.brand.com but never notbrand.com. LIKE metacharacters in
        # the operand are escaped; a domain containing % or _ is not a real host, but an
        # unescaped one would silently widen the cohort.
        domain_clause = "AND (domain = :domain_exact OR domain LIKE :domain_suffix ESCAPE '\\')"
        safe = str(domain).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        values["domain_exact"] = domain
        values["domain_suffix"] = f"%.{safe}"
    rows = await database.fetch_all(
        SELECT_CANDIDATES_SQL.format(domain_clause=domain_clause, cursor_clause=cursor_clause),
        values,
    )
    return [dict(r) for r in rows or []]


def snapshot_variants(seed_data: Any) -> Tuple[List[Dict[str, Any]], bool]:
    """(dict entries, well_formed). `well_formed` is False when the array holds a non-dict.

    The filtered list becomes the ENTIRE new array, so a scalar sitting in it would be
    silently DELETED by a write that claims only to stamp an id — the same class as the
    document-destruction blocker, one level down. The caller skips such rows.
    """
    if not isinstance(seed_data, dict):
        return [], False
    snapshot = seed_data.get("snapshot")
    if not isinstance(snapshot, dict):
        return [], False
    raw = snapshot.get("variants")
    if not isinstance(raw, list):
        return [], False
    entries = [v for v in raw if isinstance(v, dict)]
    return entries, len(entries) == len(raw)


def seed_data_of(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """None means "unreadable — skip", never "empty, safe to replace"."""
    raw = row.get("seed_data")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


async def fetch_product_js(client: Any, url: str) -> Tuple[Optional[Any], str]:
    """Return (payload, outcome). Never raises — an unreachable page is data, not an error.

    Outcomes are CLASSIFIED, not lumped: `rate_limited`/`http_403` mean our IP, `dead_handle`
    means the seed's URL is gone (6.7% of a live sample), `not_json` is the themed soft-404
    served as 200. They need different responses.
    """
    try:
        resp = await client.get(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=REQUEST_TIMEOUT_S,
            follow_redirects=True,
        )
    except Exception as exc:  # noqa: BLE001 - classified, not swallowed
        return None, f"error:{type(exc).__name__}"
    if resp.status_code == 429:
        return None, "rate_limited"
    if resp.status_code in (404, 410):
        return None, "dead_handle"
    if resp.status_code >= 400:
        return None, f"http_{resp.status_code}"
    ctype = (resp.headers.get("content-type") or "").lower()
    if "json" not in ctype and "javascript" not in ctype:
        return None, "not_json"
    try:
        return resp.json(), "ok"
    except Exception:  # noqa: BLE001
        return None, "unparseable"


async def run(
    limit: int, domain: Optional[str], apply: bool, client: Any, after: Optional[str] = None
) -> Dict[str, Any]:
    rows = await select_candidates(limit=limit, domain=domain, after=after)
    pacer = Pacer()
    outcomes: Counter = Counter()
    reasons: Counter = Counter()
    per_domain_blocks: Counter = Counter()
    stamped_total = 0
    changed_rows = 0
    conflicts = 0
    consecutive_blocks = 0
    aborted = False

    for row in rows:
        seed_data = seed_data_of(row)
        if seed_data is None:
            outcomes["unreadable_seed_data"] += 1
            continue
        variants, well_formed = snapshot_variants(seed_data)
        if not variants:
            outcomes["no_snapshot_variants"] += 1
            continue
        if not well_formed:
            outcomes["malformed_variant_element"] += 1
            continue

        page_url = row.get("canonical_url") or row.get("destination_url")
        js_url = product_js_url(page_url)
        if not js_url:
            outcomes["not_a_shopify_product_url"] += 1
            continue

        host = urlparse(js_url).hostname or (row.get("domain") or "unknown")
        if domain and not (host == domain or host.endswith(f".{domain}")):
            # `--domain` filters the domain COLUMN, but the fetch host comes from
            # canonical_url and the two can disagree. The rollout plan ("one --domain at
            # --limit 5") and the shared-NAT blast-radius argument both assume --domain
            # bounds which hosts are contacted, so it has to bound them here too.
            outcomes["host_outside_domain_filter"] += 1
            continue
        await pacer.wait(host)
        payload, outcome = await fetch_product_js(client, js_url)
        outcomes[outcome] += 1

        if _is_block(outcome):
            consecutive_blocks += 1
            per_domain_blocks[host] += 1
            if consecutive_blocks >= CONSECUTIVE_BLOCK_ABORT:
                aborted = True
                break
            continue
        consecutive_blocks = 0
        if outcome != "ok":
            continue

        live = parse_product_js(payload)
        new_variants, report = stamp_variant_ids(variants, live)
        reasons[report["reason"]] += 1
        if report["stamped"] <= 0:
            continue

        stamped_total += report["stamped"]
        changed_rows += 1
        if apply:
            written_id = await database.fetch_val(
                STAMP_UPDATE_SQL,
                {
                    "id": row["id"],
                    "variants": json.dumps(new_variants, ensure_ascii=False),
                    "platform": STOREFRONT_PLATFORM,
                    "platform_source": STOREFRONT_PLATFORM_SOURCE,
                    "updated_at": row.get("updated_at"),
                },
            )
            if written_id is None:
                # The row moved under us (the refresh job rewrites this same document) or
                # its shape changed. Counted, never forced.
                conflicts += 1
                changed_rows -= 1
                stamped_total -= report["stamped"]

    return {
        "mode": "apply" if apply else "dry_run",
        "aborted_on_block": aborted,
        # Resume point. Feed back as --after so the next run starts past everything this one
        # already walked, stampable or not. Without it the sweep re-fetches its own permanent
        # failures forever and never reaches the rest of the cohort.
        "next_cursor": (rows[-1]["id"] if rows and not aborted else None),
        "candidates": len(rows),
        "rows_with_new_ids": changed_rows,
        "variant_ids_stamped": stamped_total,
        "write_conflicts": conflicts,
        "fetch_outcomes": dict(outcomes),
        "match_reasons": dict(reasons),
        "most_blocked_domains": dict(per_domain_blocks.most_common(10)),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--domain", type=str, default=None, help="one storefront (suffix match)")
    parser.add_argument("--apply", action="store_true", help="write; omit for a dry run")
    parser.add_argument("--after", type=str, default=None,
                        help="resume past this seed id (use next_cursor from a prior run)")
    args = parser.parse_args()

    async def _main() -> Dict[str, Any]:
        await database.connect()
        try:
            async with httpx.AsyncClient() as client:
                return await run(
                    limit=args.limit, domain=args.domain, apply=args.apply,
                    client=client, after=args.after,
                )
        finally:
            await database.disconnect()

    summary = asyncio.run(_main())
    print(json.dumps(summary, indent=2, ensure_ascii=False), flush=True)
    # Non-zero on abort so a wrapper cannot read partial data as a completed sweep.
    return 1 if summary.get("aborted_on_block") else 0


if __name__ == "__main__":
    raise SystemExit(main())
