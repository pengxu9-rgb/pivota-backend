"""Backfill snapshot.variants + snapshot.extracted_at for crawl-onboarded seeds.

Why: the external-referral runtime gate (services/external_referral_readiness.py)
treats `zero_variants` as a BLOCKER, and all 2,151 tool='external_brand_crawl'
seeds were authored without a variants array (live incident 2026-07-11: the
whole crawl cohort was recalled by find_products_multi's external-seed leg but
dropped at build time -> invisible to agent search). The row-level
price/currency/availability IS the sellable unit for these single-offer D2C
PDPs, so author it as one explicit default variant (same shape the forward fix
in scripts/onboard_external_brand_from_crawl.py now writes).

extracted_at: the stale_snapshot gate (7d) falls back to row.updated_at when
snapshot.extracted_at is absent. This backfill stamps the seed's REAL last
sync time (updated_at at backfill time) as snapshot.extracted_at — it does NOT
manufacture freshness: seeds older than the gate window remain stale-blocked
until an actual re-crawl/refresh runs. It also deliberately does NOT bump
updated_at (a metadata write is not an extraction event).

Safety (WS1.2 write-incident lessons):
  - dry-run by default; --apply to write
  - --epid-prefix for a pilot slice (e.g. acropass_us_) before the full run
  - jsonb_typeof(seed_data)='object' enforced in SQL (never merge into a
    double-encoded string)
  - optimistic guard: UPDATE ... WHERE updated_at IS NOT DISTINCT FROM the row
    we read, so a concurrent writer loses nothing
  - skips any seed that already has a non-empty snapshot.variants

Usage:
  python -m scripts.backfill_crawl_seed_variants                      # dry-run all
  python -m scripts.backfill_crawl_seed_variants --epid-prefix acropass_us_ --apply
  python -m scripts.backfill_crawl_seed_variants --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import asyncpg

from scripts.onboard_external_brand_from_crawl import build_default_seed_variant

TOOL_DEFAULT = "external_brand_crawl"


def _ensure_obj(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _dsn() -> str:
    # Ops script: connect directly with asyncpg (the `databases` pool wrapper
    # times out through the Railway public proxy; plain asyncpg does not).
    # DATABASE_URL is the in-cluster host on Railway; DATABASE_PUBLIC_URL is
    # the laptop-reachable proxy (TLS must be disabled — proxy rejects it).
    import os

    return os.getenv("DATABASE_URL") or os.getenv("DATABASE_PUBLIC_URL") or ""


async def run(*, tool: str, epid_prefix: str, limit: int, apply: bool) -> None:
    conn = await asyncpg.connect(_dsn(), ssl=False)
    try:
        rows = await conn.fetch(
            """
            SELECT id, external_product_id, title, image_url,
                   price_amount, price_currency, availability,
                   updated_at, seed_data
            FROM external_product_seeds
            WHERE tool = $1
              AND status = 'active'
              AND jsonb_typeof(seed_data) = 'object'
              AND ($2 = '' OR external_product_id LIKE $3)
            ORDER BY external_product_id
            LIMIT $4
            """,
            tool,
            epid_prefix,
            f"{epid_prefix}%",
            limit,
        )
        scanned = 0
        skipped_has_variants = 0
        would_write = 0
        written = 0
        conflicts = 0
        samples = []
        for row in rows:
            r = dict(row)
            scanned += 1
            seed_data = _ensure_obj(r.get("seed_data"))
            if not seed_data:
                continue
            snapshot = _ensure_obj(seed_data.get("snapshot"))
            existing_variants = snapshot.get("variants")
            if isinstance(existing_variants, list) and existing_variants:
                skipped_has_variants += 1
                continue
            p = {
                "external_product_id": r.get("external_product_id"),
                "title": r.get("title") or snapshot.get("title"),
                "image_url": r.get("image_url"),
                "price_amount": (
                    float(r["price_amount"]) if r.get("price_amount") is not None else None
                ),
                "price_currency": r.get("price_currency"),
            }
            snapshot["variants"] = [build_default_seed_variant(p)]
            if not str(snapshot.get("extracted_at") or "").strip():
                updated_at = r.get("updated_at")
                snapshot["extracted_at"] = (
                    updated_at.isoformat() if hasattr(updated_at, "isoformat") else str(updated_at or "")
                )
            seed_data["snapshot"] = snapshot
            would_write += 1
            if len(samples) < 3:
                samples.append(
                    {
                        "id": r.get("id"),
                        "variant": snapshot["variants"][0],
                        "extracted_at": snapshot["extracted_at"],
                    }
                )
            if apply:
                tag = await conn.execute(
                    """
                    UPDATE external_product_seeds
                    SET seed_data = CAST($1 AS jsonb)
                    WHERE id = $2
                      AND updated_at IS NOT DISTINCT FROM $3
                    """,
                    json.dumps(seed_data, ensure_ascii=False, default=str),
                    r.get("id"),
                    r.get("updated_at"),
                )
                if str(tag).endswith("1"):
                    written += 1
                else:
                    conflicts += 1
        print(
            json.dumps(
                {
                    "mode": "apply" if apply else "dry_run",
                    "tool": tool,
                    "epid_prefix": epid_prefix or None,
                    "scanned": scanned,
                    "skipped_has_variants": skipped_has_variants,
                    "would_write": would_write,
                    "written": written,
                    "conflicts": conflicts,
                    "samples": samples,
                },
                ensure_ascii=False,
                indent=1,
                default=str,
            )
        )
    finally:
        await conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tool", default=TOOL_DEFAULT)
    parser.add_argument("--epid-prefix", default="")
    parser.add_argument("--limit", type=int, default=5000)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    asyncio.run(run(tool=args.tool, epid_prefix=args.epid_prefix, limit=args.limit, apply=args.apply))


if __name__ == "__main__":
    main()
