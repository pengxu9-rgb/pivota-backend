"""Reset `canonical_url` to `destination_url` where the stored canonical locks a seed out.

`canonical_url` is the SERVED URL (`destination_of` reads it first). The seed refresh used to
write the page's self-declared canonical on every attempt, and a store's canonical tag can name
a sibling shade, another host or another locale. Once stored, the refresh (which fetches
`destination_url`) can never read that row again, and the liveness sweep and readiness gate
judge a page the buyer is not sent to -- the click is minted from `destination_url`. The refresh
no longer writes such a canonical (`_next_served_canonical`); this clears the ones it already
wrote. Measured 2026-09-26 over 21,664 active seeds: 581 name a different product handle
(fentybeauty 501), 524 the same handle on another host / locale / query, 257 have no product
handle on one side.

DRY RUN BY DEFAULT: counts by category and domain, plus samples. `--apply` sets
`canonical_url = destination_url` on each row whose canonical is still exactly what was read
(a concurrent refresh wins), and records `seed_data.canonical_reset = {from, at}` so every
reset can be undone. It writes the database but fetches nothing, so it needs no crawl subnet:

    scripts/ops/run_oneoff_job.sh scripts/ops/reset_unreadable_canonical_urls.py
    scripts/ops/run_oneoff_job.sh scripts/ops/reset_unreadable_canonical_urls.py --apply

`--category` narrows to one class (default: all three).

THE SERVED PRODUCT ID IS PINNED. `routes/agent_shop_gateway` serves `seed_data.external_product_id`
and, when a seed has none, `ext_` + sha256(canonical_url or destination_url) -- so resetting the
canonical would change that seed's served id. `--apply` writes the id the gateway serves TODAY
into `seed_data.external_product_id` where it is missing (same UPDATE), and the dry run counts
those rows as `served_id_pinned`.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from db.database import database  # noqa: E402

CATEGORIES = ("different_product", "same_product_elsewhere", "no_handle")


def classify(destination_url: Optional[str], canonical_url: Optional[str]) -> Optional[str]:
    """None when the stored canonical is harmless (absent, or the same destination)."""
    from routes.employee_products import _same_destination
    from services.outbound_warm_handoff import extract_product_handle

    if not canonical_url or not destination_url or _same_destination(destination_url, canonical_url):
        return None
    wanted = (extract_product_handle(destination_url) or "").lower()
    stored = (extract_product_handle(canonical_url) or "").lower()
    if not wanted or not stored:
        return "no_handle"
    return "same_product_elsewhere" if wanted == stored else "different_product"


async def _select(categories: List[str]) -> List[Dict[str, Any]]:
    rows = await database.fetch_all(
        """
        SELECT id, domain, destination_url, canonical_url,
               COALESCE(seed_data->>'external_product_id', '') AS seed_external_product_id
        FROM external_product_seeds
        WHERE status = 'active' AND canonical_url IS NOT NULL AND destination_url IS NOT NULL
        ORDER BY domain, id
        """
    )
    out: List[Dict[str, Any]] = []
    for r in rows:
        row = dict(r)
        category = classify(row["destination_url"], row["canonical_url"])
        if category in categories:
            out.append(row | {"category": category})
    return out


def served_external_product_id(canonical_url: Optional[str], destination_url: Optional[str]) -> str:
    """The id `agent_shop_gateway` serves for a seed with no `seed_data.external_product_id`."""
    url = str(canonical_url or destination_url or "").strip()
    return "ext_" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:24] if url else ""


_RESET_SQL = """
UPDATE external_product_seeds
SET canonical_url = destination_url,
    seed_data = jsonb_set(
        jsonb_set(
            COALESCE(seed_data::jsonb, '{}'::jsonb),
            '{canonical_reset}',
            jsonb_build_object(
                'from', CAST(:old AS TEXT),
                'at', CAST(:at AS TEXT),
                'pinned_external_product_id', CAST(:served_id AS TEXT)
            )
        ),
        '{external_product_id}',
        CASE WHEN COALESCE(seed_data::jsonb->>'external_product_id', '') = ''
             THEN to_jsonb(CAST(:served_id AS TEXT))
             ELSE seed_data::jsonb->'external_product_id' END
    ),
    updated_at = NOW()
WHERE id = :id AND canonical_url = :old
"""


async def _run(args: argparse.Namespace) -> Dict[str, Any]:
    await database.connect()
    try:
        rows = await _select(args.category or list(CATEGORIES))
        if args.limit:
            rows = rows[: args.limit]
        summary: Dict[str, Any] = {
            "mode": "apply" if args.apply else "dry_run",
            "selected": len(rows),
            "by_category": dict(collections.Counter(r["category"] for r in rows)),
            # Seeds with no seed_data.external_product_id: the gateway serves an id hashed from
            # canonical_url, which --apply pins (see the module docstring).
            "served_id_pinned": sum(1 for r in rows if not r.get("seed_external_product_id")),
            "top_domains": collections.Counter(r["domain"] for r in rows).most_common(15),
            "samples": [
                {"id": r["id"], "category": r["category"], "destination": r["destination_url"], "canonical": r["canonical_url"]}
                for r in rows[:12]
            ],
        }
        if not args.apply:
            return summary
        reset = skipped = 0
        # Bound, not formatted in SQL: a `HH24:MI:SS` literal would parse as binds `:MI`/`:SS`.
        at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for r in rows:
            served_id = served_external_product_id(r["canonical_url"], r["destination_url"])
            result = await database.fetch_one(
                _RESET_SQL + " RETURNING id",
                {"id": r["id"], "old": r["canonical_url"], "at": at, "served_id": served_id},
            )
            if result:
                reset += 1
            else:
                skipped += 1  # the canonical moved since it was read; leave the newer value
        summary["reset"] = reset
        summary["skipped_changed_since_read"] = skipped
        return summary
    finally:
        await database.disconnect()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--category", action="append", choices=CATEGORIES, help="repeatable; default all")
    parser.add_argument("--limit", type=int, default=0, help="0 = every selected row")
    parser.add_argument("--apply", action="store_true", help="WRITE; default is a dry run")
    return parser


def main() -> int:
    args = _parser().parse_args()
    print(json.dumps(asyncio.run(_run(args)), ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
