"""Bulk re-snapshot of product_quality_snapshot over the live catalog
(Fix Plan G — T2 full-cohort rollout, post-T1 enrichment).

`scripts/resnapshot_pilot_quality.py` re-snapshots explicit keys through
`full_quality_eval` — one INSERT round-trip per product, which over the public
proxy (~1 statement/s) cannot cover the 9.4K live cohort. This script is the
set-based twin for the FULL cohort:

  - keyset-scans the live, non-demo catalog (same cohort predicate as the T1
    backfill, WITHOUT the llm_attributes-NULL guard — every live row gets a
    fresh snapshot);
  - computes content_quality + the re-derived model_readiness IN PROCESS with
    the exact serving code path (build_quality_payload -> preview_quality),
    reading the durable resolved_vertical + llm_attributes;
  - INSERTs one snapshot row per product per batch through a single
    unnest(...) statement (append-only history; prior rows untouched →
    reversible by ignoring/deleting model_version = 'structural_depth.g1').

Resumable: re-running appends fresh snapshots (latest-wins readers pick the
newest row); --start-after resumes the keyset mid-catalog after a crash.

Usage:
  railway run bash -c 'DATABASE_URL="$DATABASE_PUBLIC_URL" PYTHONPATH="$PWD" \
      python3.11 -m scripts.resnapshot_quality_bulk --dry-run'
  ... (apply)      python3.11 -m scripts.resnapshot_quality_bulk
"""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any, Dict, List, Optional

from db.database import database
from services.product_quality_service import (
    DEFAULT_QUALITY_RULES_VERSION,
    build_quality_payload,
    preview_quality,
)

MODEL_VERSION = "structural_depth.g1"

# Same demo exclusions as the T1 backfill (scripts/_utils/demoExclusions.cjs).
_DEMO_MERCHANTS: List[str] = [
    "merch_efbc46b4619cfbdf", "merch_bbd34645bc1950cc", "merch_test_ownist_001",
    "merch_shopify_00d4a720d67d96c5dcba", "merch_shopify_0584b37f7a8be00a5223",
    "merch_shopify_b20b5797f4181983c177",
]

_SELECT_SQL = """
    SELECT product_key, merchant_id, platform, source_product_id,
           title, description, product_type, category, category_path,
           brand, image_url, product_payload, resolved_vertical, llm_attributes
    FROM catalog_products cp
    WHERE cp.suppression_reason IS NULL
      AND COALESCE(cp.source_domain, '') NOT LIKE 'pivota-review-demo%'
      AND cp.merchant_id <> ALL(:demo_merchants)
      AND cp.product_key > :cursor
    ORDER BY cp.product_key ASC
    LIMIT :batch_size
"""

# One set-based INSERT per batch: zips five arrays through unnest. Append-only;
# never updates or deletes an existing snapshot row.
_INSERT_BATCH_SQL = f"""
    INSERT INTO product_quality_snapshot
        (merchant_id, platform, platform_product_id,
         content_quality_score, model_readiness_score,
         rules_version, model_version)
    SELECT v.mid, v.plat, v.pid, v.cq, v.mr,
           '{DEFAULT_QUALITY_RULES_VERSION}', '{MODEL_VERSION}'
    FROM unnest(
        CAST(:mids AS text[]), CAST(:plats AS text[]), CAST(:pids AS text[]),
        CAST(:cqs AS float8[]), CAST(:mrs AS float8[])
    ) AS v(mid, plat, pid, cq, mr)
"""


def _price_from_payload(product_payload: Any) -> Optional[float]:
    data = product_payload
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (ValueError, TypeError):
            return None
    if not isinstance(data, dict):
        return None
    for key in ("price", "price_value", "base_price_value", "list_price"):
        val = data.get(key)
        if isinstance(val, (int, float)) and val > 0:
            return float(val)
    return None


def _row_to_product(row: Dict[str, Any]) -> Dict[str, Any]:
    llm_attributes = row.get("llm_attributes")
    if isinstance(llm_attributes, str):
        try:
            llm_attributes = json.loads(llm_attributes)
        except (ValueError, TypeError):
            llm_attributes = None
    return {
        "title": row.get("title"),
        "description": row.get("description"),
        "product_type": row.get("product_type"),
        "category": row.get("category"),
        "category_path": row.get("category_path"),
        "brand": row.get("brand"),
        "image_url": row.get("image_url"),
        "price": _price_from_payload(row.get("product_payload")),
        "resolved_vertical": row.get("resolved_vertical"),
        "llm_attributes": llm_attributes,
    }


async def _connect_if_needed(db: Any) -> bool:
    was = bool(getattr(db, "is_connected", False))
    if not was and callable(getattr(db, "connect", None)):
        attempt = 0
        while True:
            try:
                await db.connect()
                break
            except Exception as exc:  # noqa: BLE001
                attempt += 1
                if attempt > 5:
                    raise
                print(f"WARN db.connect retry {attempt}: {exc!r}", flush=True)
                await asyncio.sleep(1.5 * attempt)
    return was


async def _with_retry(coro_factory, *, max_retries: int, base_delay: float, label: str):
    attempt = 0
    while True:
        try:
            return await coro_factory()
        except Exception as exc:  # noqa: BLE001
            attempt += 1
            if attempt > max_retries:
                raise RuntimeError(f"{label} failed after {max_retries} retries: {exc!r}") from exc
            delay = base_delay * (2 ** (attempt - 1))
            print(f"WARN: {label} errored (attempt {attempt}/{max_retries}): {exc!r}; "
                  f"retrying in {delay:.1f}s", flush=True)
            await asyncio.sleep(delay)


def _dist(scores: List[float]) -> Dict[str, Any]:
    if not scores:
        return {}
    s = sorted(scores)
    n = len(s)
    return {
        "n": n,
        "avg": round(sum(s) / n, 2),
        "min": s[0], "p25": s[n // 4], "median": s[n // 2],
        "p75": s[3 * n // 4], "max": s[-1],
    }


async def _drive(args: argparse.Namespace, *, db: Any = database) -> Dict[str, Any]:
    was = await _connect_if_needed(db)
    scanned = 0
    inserted = 0
    batches = 0
    cursor = args.start_after or ""
    readiness_scores: List[float] = []
    try:
        while True:
            if args.max_batches and batches >= args.max_batches:
                break
            rows = await _with_retry(
                lambda: db.fetch_all(_SELECT_SQL, {
                    "cursor": cursor, "batch_size": args.batch_size,
                    "demo_merchants": _DEMO_MERCHANTS,
                }),
                max_retries=args.max_retries, base_delay=args.retry_base_delay,
                label="fetch_batch",
            )
            rows = [dict(r) for r in rows]
            if not rows:
                break
            batches += 1

            mids: List[str] = []
            plats: List[str] = []
            pids: List[str] = []
            cqs: List[float] = []
            mrs: List[float] = []
            for row in rows:
                scanned += 1
                preview = preview_quality(build_quality_payload(_row_to_product(row)))
                mr = float(preview.get("model_readiness_score") or 0.0)
                cq = float(preview.get("content_quality_score") or 0.0)
                readiness_scores.append(mr)
                mids.append(str(row.get("merchant_id") or ""))
                plats.append(str(row.get("platform") or ""))
                pids.append(str(row.get("source_product_id") or ""))
                cqs.append(cq)
                mrs.append(mr)

            if not args.dry_run and mids:
                await _with_retry(
                    lambda m=mids, p=plats, i=pids, c=cqs, r=mrs: db.execute(
                        _INSERT_BATCH_SQL,
                        {"mids": m, "plats": p, "pids": i, "cqs": c, "mrs": r},
                    ),
                    max_retries=args.max_retries, base_delay=args.retry_base_delay,
                    label="insert_batch",
                )
                inserted += len(mids)

            cursor = rows[-1]["product_key"]
            print(f"batch {batches}: scanned={scanned} inserted={inserted} "
                  f"cursor={cursor!r}", flush=True)
    finally:
        if not was and bool(getattr(db, "is_connected", False)):
            await db.disconnect()

    return {
        "dry_run": args.dry_run,
        "scanned": scanned,
        "inserted": inserted,
        "batches": batches,
        "model_version": MODEL_VERSION,
        "readiness_distribution": _dist(readiness_scores),
        "readiness_gt0": sum(1 for s in readiness_scores if s > 0),
    }


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true",
                   help="Compute the distribution; insert nothing.")
    p.add_argument("--batch-size", type=int, default=500)
    p.add_argument("--max-batches", type=int, default=0)
    p.add_argument("--start-after", default="",
                   help="Resume keyset: skip product_key <= this value.")
    p.add_argument("--max-retries", type=int, default=5)
    p.add_argument("--retry-base-delay", type=float, default=1.0)
    return p.parse_args(argv)


def main() -> int:
    args = _parse_args()
    report = asyncio.run(_drive(args))
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
