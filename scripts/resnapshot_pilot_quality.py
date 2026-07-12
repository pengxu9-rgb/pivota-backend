"""Re-snapshot product_quality_snapshot for the structural-depth pilot cohort
(Fix Plan G — T2).

After the T1 pilot writes llm_attributes for a set of products, this recomputes
their quality snapshot so the re-derived model_readiness_score (which now reads
the base structure + resolved_vertical + llm_attributes) is persisted and can be
compared before/after. STRICTLY pilot-scoped: it only touches the explicit
product_keys handed to it (never the whole catalog), matching the plan's
"re-snapshot ONLY the 100 pilot products".

For each product_key it:
  1. loads the catalog row (title/description/image/brand/category + the durable
     resolved_vertical and llm_attributes),
  2. reads the CURRENT latest snapshot readiness (the "before"),
  3. builds the quality payload (build_quality_payload threads the new signals),
  4. calls full_quality_eval — which appends a new snapshot row — tagged
     model_version so the re-snapshot cohort is identifiable,
  5. records before/after.

Writes: one new product_quality_snapshot row per key (append-only history; the
old rows are untouched, so it is trivially reversible — the prior latest row is
still there and can be re-promoted by ignoring the tagged model_version).

Usage:
  railway run bash -c 'DATABASE_URL="$DATABASE_PUBLIC_URL" PYTHONPATH="$PWD" \
    python3.11 -m scripts.resnapshot_pilot_quality --keys-file pilot_keys.txt'
  ... --dry-run   # compute before/after in memory, write NOTHING
"""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any, Dict, List, Optional

from db.database import database
from services.product_quality_service import build_quality_payload, full_quality_eval

_MODEL_VERSION = "structural_depth.g1"

_CATALOG_SQL = """
    SELECT product_key, merchant_id, platform, source_product_id,
           title, description, product_type, category, category_path,
           brand, image_url, product_payload, resolved_vertical, llm_attributes
    FROM catalog_products
    WHERE product_key = :product_key
"""

# Latest existing snapshot readiness for the "before" reading.
_BEFORE_SQL = """
    SELECT model_readiness_score, content_quality_score, snapshot_date
    FROM product_quality_snapshot
    WHERE merchant_id = :merchant_id
      AND platform = :platform
      AND platform_product_id = :platform_product_id
    ORDER BY snapshot_date DESC, id DESC
    LIMIT 1
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
    """Shape a catalog row into the dict build_quality_payload consumes, carrying
    the new durable signals so readiness sees them."""
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


async def _resnapshot_one(db: Any, product_key: str, *, dry_run: bool) -> Dict[str, Any]:
    row = await db.fetch_one(_CATALOG_SQL, {"product_key": product_key})
    if row is None:
        return {"product_key": product_key, "status": "not_found"}
    row = dict(row)
    merchant_id = row.get("merchant_id")
    platform = row.get("platform")
    platform_product_id = row.get("source_product_id")

    before_row = await db.fetch_one(_BEFORE_SQL, {
        "merchant_id": merchant_id, "platform": platform,
        "platform_product_id": platform_product_id,
    })
    before = dict(before_row).get("model_readiness_score") if before_row else None

    payload = build_quality_payload(_row_to_product(row))

    if dry_run:
        from services.product_quality_service import preview_quality
        preview = preview_quality(payload)
        after = preview.get("model_readiness_score")
    else:
        result = await full_quality_eval(
            merchant_id=merchant_id, platform=platform,
            platform_product_id=platform_product_id, geo_code=None,
            payload=payload, model_version=_MODEL_VERSION,
        )
        after = result.get("model_readiness_score")

    return {
        "product_key": product_key,
        "status": "ok",
        "readiness_before": before,
        "readiness_after": after,
        "resolved_vertical": row.get("resolved_vertical"),
        "llm_attribute_field_count": payload.get("llm_attribute_field_count"),
    }


async def _drive(args: argparse.Namespace, *, db: Any = database) -> Dict[str, Any]:
    keys = _load_keys(args)
    was = await _connect_if_needed(db)
    results: List[Dict[str, Any]] = []
    try:
        for key in keys:
            results.append(await _resnapshot_one(db, key, dry_run=args.dry_run))
    finally:
        if not was and bool(getattr(db, "is_connected", False)):
            await db.disconnect()

    ok = [r for r in results if r["status"] == "ok"]
    befores = [r["readiness_before"] for r in ok if r["readiness_before"] is not None]
    afters = [r["readiness_after"] for r in ok if r["readiness_after"] is not None]
    return {
        "dry_run": args.dry_run,
        "requested": len(keys),
        "resnapshotted": len(ok),
        "not_found": [r["product_key"] for r in results if r["status"] == "not_found"],
        "avg_readiness_before": round(sum(befores) / len(befores), 2) if befores else None,
        "avg_readiness_after": round(sum(afters) / len(afters), 2) if afters else None,
        "had_prior_snapshot": len(befores),
        "moved_up": sum(
            1 for r in ok
            if r["readiness_after"] is not None
            and (r["readiness_before"] or 0.0) < r["readiness_after"]
        ),
        "results": results,
    }


def _load_keys(args: argparse.Namespace) -> List[str]:
    keys: List[str] = []
    if args.keys_file:
        with open(args.keys_file, "r", encoding="utf-8") as fh:
            content = fh.read()
        for tok in content.replace(",", "\n").splitlines():
            tok = tok.strip()
            if tok:
                keys.append(tok)
    if args.keys:
        keys.extend(k.strip() for k in args.keys.split(",") if k.strip())
    # de-dup, preserve order
    seen = set()
    out = []
    for k in keys:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--keys-file", help="File of product_keys (newline/comma separated).")
    p.add_argument("--keys", help="Comma-separated product_keys.")
    p.add_argument("--dry-run", action="store_true", help="Compute before/after; write nothing.")
    return p.parse_args(argv)


def main() -> int:
    args = _parse_args()
    report = asyncio.run(_drive(args))
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
