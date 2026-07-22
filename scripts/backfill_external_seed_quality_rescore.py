"""Re-score already-onboarded external_brand_crawl products through the
ingredient-aware quality payload (this PR).

Products onboarded before this fix scored ~50 and stalled at low_quality: the
scorer never saw their ingredients and their product_type was null. This
backfill pulls each product's fields from catalog_products, its price from
external_product_seeds, and its INCI from beauty_sku_ingredients, rebuilds the
payload with build_servable_quality_payload(category=category_kind, raw_inci=...),
and re-runs make_external_seed_servable -> fresh product_quality_snapshot +
serving-eligibility recompute.

Idempotent + RESUMABLE (skips products already on the source-backed rules
version) + RESILIENT (one product's failure never aborts the run) + a
consecutive-failure circuit breaker for dead connections.

Designed to run as a Railway job against the INTERNAL DB:
  Dry-run:  python -m scripts.backfill_external_seed_quality_rescore
  Apply:    python -m scripts.backfill_external_seed_quality_rescore --apply [--limit N]

Local run over the public proxy (db.database reads DATABASE_URL, which is the
internal railway.internal host under `railway run` and won't resolve locally):
  railway run -- bash -c 'DATABASE_URL="$DATABASE_PUBLIC_URL" \
    python -m scripts.backfill_external_seed_quality_rescore --apply'
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db.database import database
from services.external_seed_servability import (
    build_servable_quality_payload,
    make_external_seed_servable,
)
from services.product_quality_service import SOURCE_BACKED_COMPONENTS_RULES_VERSION

TOOL = "external_brand_crawl"

# Scope: ANY seed-attached product, not just `external_brand_crawl::%`. The
# original filter matched only one ingest path and silently skipped 2,404 of the
# 4,072 seed-attached beauty rows (measured 2026-07-22) — they carry the same
# payload shape and the same ~50 stalled score, they just arrived via a different
# source_ref. `--tool-prefix` restores the old narrow behavior when needed.
FETCH = """
    SELECT p.product_key, p.source_product_id, p.title, p.description, p.brand,
           p.product_type, p.category_kind, p.image_url,
           eps.price_amount, bsi.raw_inci,
           eps.seed_data -> 'pdp_details_sections' AS pdp_details_sections
    FROM catalog_products p
    JOIN external_product_seeds eps ON eps.attached_product_key = p.product_key
    LEFT JOIN beauty_sku_ingredients bsi
           ON bsi.sku_key = p.product_key || '::canonical'
    WHERE (
        CAST(:source_prefix AS TEXT) IS NULL
        OR p.source_ref LIKE CAST(:source_prefix AS TEXT)
    )
    ORDER BY p.product_key
"""


def _coerce_sections(value: Any) -> Optional[list]:
    """`seed_data->'pdp_details_sections'` arrives as JSON (str or list)."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            return None
    return list(value) if isinstance(value, (list, tuple)) else None


async def _rescored_ids() -> set:
    rows = await database.fetch_all(
        "SELECT DISTINCT platform_product_id AS pid FROM product_quality_snapshot "
        "WHERE rules_version = :v",
        {"v": SOURCE_BACKED_COMPONENTS_RULES_VERSION},
    )
    return {dict(r)["pid"] for r in rows}


async def run(
    apply: bool,
    limit: Optional[int],
    *,
    source_prefix: Optional[str] = None,
    force: bool = False,
) -> None:
    await database.connect()
    try:
        rows = [
            dict(r)
            for r in await database.fetch_all(FETCH, {"source_prefix": source_prefix})
        ]
        done = set() if force else await _rescored_ids()
        todo = [r for r in rows if r["source_product_id"] not in done]
        if limit:
            todo = todo[:limit]
        with_inci = sum(1 for r in rows if (r.get("raw_inci") or "").strip())
        with_sections = sum(1 for r in rows if _coerce_sections(r.get("pdp_details_sections")))
        print(
            f"batch={len(rows)}  already-rescored={len(done)}  "
            f"to-rescore={len(todo)}  (with INCI: {with_inci}, "
            f"with pdp_details_sections: {with_sections})",
            flush=True,
        )

        if not apply:
            for r in todo[:5]:
                has_inci = "Y" if (r.get("raw_inci") or "").strip() else "N"
                has_sec = "Y" if _coerce_sections(r.get("pdp_details_sections")) else "N"
                print(f"  would rescore {r['source_product_id']} inci={has_inci} "
                      f"sections={has_sec} cat={r['category_kind']} "
                      f":: {(r['title'] or '')[:40]}")
            print("DRY-RUN — pass --apply to write.")
            return

        ok = fail = 0
        consec = 0
        for i, r in enumerate(todo, 1):
            epid = r["source_product_id"]
            if consec >= 5:
                print(f"[ABORT] {consec} consecutive failures — connection likely "
                      f"dead. Re-run to resume (already-rescored are skipped).", flush=True)
                break
            try:
                qp = build_servable_quality_payload(
                    title=r["title"], description=r["description"],
                    price=r["price_amount"], image_url=r["image_url"],
                    brand=r["brand"], product_type=r["product_type"],
                    category=r["category_kind"], raw_inci=r["raw_inci"],
                    pdp_details_sections=_coerce_sections(r.get("pdp_details_sections")),
                )
                # per-product timeout so a dead socket errors out, never hangs
                await asyncio.wait_for(
                    make_external_seed_servable(
                        product_key=r["product_key"], seed_id=f"{TOOL}::{epid}",
                        source_product_id=epid, quality_payload=qp,
                        reason="rescore_ingredient_aware",
                    ),
                    timeout=45,
                )
                ok += 1
                consec = 0
            except Exception as e:  # noqa: BLE001 -- isolate per-product failures
                fail += 1
                consec += 1
                print(f"  FAIL {epid}: {type(e).__name__}: {str(e)[:80]}", flush=True)
            if i % 100 == 0:
                print(f"  {i}/{len(todo)} ok={ok} fail={fail}", flush=True)
        print(f"\nDONE: rescored ok={ok} fail={fail} (of {len(todo)})")
    finally:
        await database.disconnect()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write (default: dry-run)")
    ap.add_argument("--limit", type=int, default=None, help="cap rows this run")
    ap.add_argument(
        "--tool-prefix",
        default=None,
        help="restrict to one ingest path, e.g. 'external_brand_crawl::%%' "
             "(default: ALL seed-attached products)",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="re-score even products already on the current rules version "
             "(needed when the payload shape changes, e.g. pdp_details_sections)",
    )
    args = ap.parse_args()
    asyncio.run(
        run(args.apply, args.limit, source_prefix=args.tool_prefix, force=args.force)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
