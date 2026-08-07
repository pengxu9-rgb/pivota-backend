#!/usr/bin/env python3
"""W2 follow-up — re-derive `external_product_seeds.seller_ref` for RETAILER
domains.

The A9-3 seeds backfill keyed every seed on (brand, etld1) — correct for
brand-direct storefronts, wrong for retailer domains: ulta.com's seeds carry
per-BRAND observed sellers (merch_obs_039b8cd5… ×45, merch_obs_dbc4606f… ×17,
…), fragmenting one retailer into many merchants. W2 (pivota-backend #1686)
ratified retailer keying on etld1 ALONE; this script re-points the seeds so
the mirror's seller_ref-driven minting and the R3 flip agree with it.

Scope, deliberately narrow:
  - active seeds whose row-owned domain (domain -> destination_url host ->
    canonical_url host, first RESOLVABLE) classifies as a known retailer;
  - the new value is exactly `resolve_seed_seller_identity(...)['merchant_id']`;
  - rows already carrying the correct retailer id are untouched;
  - seeds that do not classify retailer are NEVER touched — brand-direct
    keying is unchanged, and an unresolvable domain leaves the seed alone
    (its seller_ref, right or wrong, is a question for its own lane).

Writes are chunked (the Railway public proxy drops long statements), each
chunk is a single UPDATE ... WHERE id = ANY(...) with the SAME target value,
and re-running is a no-op for already-updated rows (converge, don't toggle).

DRY-RUN by default; --execute required for writes.

  DB_SSL_NO_VERIFY=1 DB_POOL_ACQUIRE_TIMEOUT_SECONDS=30 DB_POOL_MIN_SIZE=1 \\
    DATABASE_URL=... .venv/bin/python -m scripts.rederive_retailer_seed_seller_refs
  ... --execute      # after review
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.seller_identity import resolve_seed_seller_identity  # noqa: E402

CHUNK = 200


def seed_domain_candidates(row: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    dom = str(row.get("domain") or "").strip()
    if dom:
        out.append(dom)
    for url_col in ("destination_url", "canonical_url"):
        url = str(row.get(url_col) or "").strip()
        if url:
            host = urlparse(url).hostname or ""
            if host:
                out.append(host)
    return out


def plan_seed(row: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """Return {seed_id, old, new} when the seed's domain classifies RETAILER
    and its seller_ref is not already the retailer identity; else None."""
    for cand in seed_domain_candidates(row):
        try:
            ident = resolve_seed_seller_identity(brand=str(row.get("brand") or ""), domain=cand)
        except ValueError:
            continue
        if ident["kind"] != "retailer":
            return None  # brand-direct: out of scope, untouched
        old = str(row.get("seller_ref") or "")
        if old == ident["merchant_id"]:
            return None
        return {"seed_id": str(row.get("id")), "old": old,
                "new": ident["merchant_id"], "registrable": ident["registrable"]}
    return None


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true", help="write (default: dry-run)")
    args = ap.parse_args()

    from db.database import database

    await database.connect()
    try:
        rows = await database.fetch_all(
            # seed_data carries brand for seeds without a column (A9-4 note);
            # brand is only used for classification tie-breaks, retailer keying
            # ignores it by design.
            "SELECT id, domain, destination_url, canonical_url, seller_ref, "
            "seed_data->>'brand' AS brand "
            "FROM external_product_seeds WHERE status = 'active'"
        )
        plans = []
        for r in rows or []:
            p = plan_seed(dict(r))
            if p:
                plans.append(p)

        by_target: Dict[str, List[str]] = defaultdict(list)
        for p in plans:
            by_target[p["new"]].append(p["seed_id"])
        summary = {
            "mode": "execute" if args.execute else "dry_run",
            "seeds_scanned": len(rows or []),
            "seeds_to_rederive": len(plans),
            "by_registrable": dict(Counter(p["registrable"] for p in plans)),
            "distinct_old_refs_replaced": len({p["old"] for p in plans if p["old"]}),
            "distinct_new_refs": len(by_target),
        }
        print(json.dumps(summary, indent=1))

        if not args.execute:
            return
        updated = 0
        for target, seed_ids in by_target.items():
            for i in range(0, len(seed_ids), CHUNK):
                chunk = seed_ids[i:i + CHUNK]
                await database.execute(
                    "UPDATE external_product_seeds SET seller_ref = :target, updated_at = NOW() "
                    "WHERE id = ANY(:ids) AND coalesce(seller_ref, '') <> :target",
                    {"target": target, "ids": chunk},
                )
                updated += len(chunk)
                print(json.dumps({"event": "chunk_done", "target": target,
                                  "rows": len(chunk), "total_sent": updated}))
        print(json.dumps({"event": "complete", "rows_sent": updated}))
    finally:
        await database.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
