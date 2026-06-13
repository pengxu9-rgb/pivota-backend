"""Pivota-vs-native decision-grade eval runner for cohort SKUs.

For each product_key, assemble the live agent record (beauty vertical payload +
resolved best US offer) and score it on the decision-grade rubric both as Pivota
serves it and as native catalog retrieval would -- printing the per-dimension
advantage. This operationalizes the differentiated-data thesis for the K-beauty
cohort (One by Zero / Aruen skincare, etc.).

Alternatives are passed empty here: the comparison dimension is supply-gated
(it unlocks once multiple brands share a category), so the runner reports it as
a known gap rather than inferring it.

Usage:
  python -m scripts.run_kbeauty_decision_grade_eval --product-key pk1 --product-key pk2
  python -m scripts.run_kbeauty_decision_grade_eval --product-keys-file keys.txt
"""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any, Dict, List, Optional

from db.database import database
from models.catalog import PivotOffersResolveRequest
from services.decision_grade_eval import compare_decision_grade, eval_record_from_payload
from services.pivot_query_service import (
    _fetch_beauty_vertical_payload,
    resolve_pivot_offers,
)


async def _connect_if_needed(db: Any) -> bool:
    was_connected = bool(getattr(db, "is_connected", False))
    if not was_connected:
        connect = getattr(db, "connect", None)
        if callable(connect):
            await connect()
    return was_connected


async def _disconnect_if_needed(db: Any, was_connected: bool) -> None:
    if not was_connected and bool(getattr(db, "is_connected", False)):
        disconnect = getattr(db, "disconnect", None)
        if callable(disconnect):
            await disconnect()


async def _resolve_representative_sku_key(product_key: str, *, db: Any = database) -> Optional[str]:
    """Resolve a SKU that carries this product's ingredient data.

    The beauty payload fetch HARD-GATES its beauty_sku_ingredients query on
    sku_key: passing None silently drops active_ingredients, so the eval's
    `find` dimension (which requires key actives / key ingredients) could never
    pass. Resolve the SKU that actually holds the ingredient row so the live
    record is assembled the way it is served. None when the product has no
    ingredient data (then there is genuinely nothing to load)."""
    row = await db.fetch_one(
        """
        SELECT sku_key
        FROM beauty_sku_ingredients
        WHERE product_key = :product_key
          AND sku_key IS NOT NULL
        LIMIT 1
        """,
        {"product_key": product_key},
    )
    sku_key = dict(row).get("sku_key") if row else None
    return str(sku_key) if sku_key else None


async def _resolve_alternatives(
    product_key: str, category_kind: Optional[str], *, db: Any = database
) -> List[Dict[str, Any]]:
    """Same-category, DIFFERENT-BRAND products that carry structured actives --
    the real cross-brand comparison set.

    The `compare` dimension is supply-gated: it can only unlock once a category
    holds more than one brand whose ingredient structure is comparable. We
    require the alternative to itself carry a non-empty active_ingredients_json
    (i.e. it is structured, not a shell) and a different brand, so this returns
    a non-empty set exactly when an honest cross-brand comparison exists -- which
    is precisely the structure native catalog retrieval lacks. Empty (compare
    stays gated) until a second structured brand lands in the category."""
    if not category_kind:
        return []
    rows = await db.fetch_all(
        """
        SELECT DISTINCT cp.product_key, cp.title, cp.brand
        FROM catalog_products cp
        JOIN beauty_sku_ingredients bsi ON bsi.product_key = cp.product_key
        WHERE cp.category_kind = :category_kind
          AND cp.product_key <> :product_key
          AND bsi.active_ingredients_json IS NOT NULL
          AND jsonb_array_length(bsi.active_ingredients_json) > 0
          AND lower(coalesce(cp.brand, '')) <> coalesce(
                (SELECT lower(brand) FROM catalog_products WHERE product_key = :product_key), '')
        LIMIT 8
        """,
        {"category_kind": category_kind, "product_key": product_key},
    )
    return [dict(r) for r in rows]


async def _assemble_record(product_key: str, *, db: Any = database) -> Dict[str, Any]:
    sku_key = await _resolve_representative_sku_key(product_key, db=db)
    payload = await _fetch_beauty_vertical_payload(product_key, sku_key)
    offers = await resolve_pivot_offers(PivotOffersResolveRequest(product_key=product_key))
    best = offers.best_us_offer.model_dump() if offers.best_us_offer else None
    alternatives = await _resolve_alternatives(product_key, payload.get("category_kind"), db=db)
    return eval_record_from_payload(payload, best_us_offer=best, alternatives=alternatives)


def _per_sku_row(product_key: str, comparison: Dict[str, Any]) -> Dict[str, Any]:
    pivota = comparison["pivota"]
    native = comparison["native"]
    return {
        "product_key": product_key,
        "category_kind": pivota.category_kind or "uncategorized",
        "pivota_overall": pivota.overall,
        "native_overall": native.overall,
        "overall_advantage": comparison["overall_advantage"],
        "pivota_decision_grade": pivota.is_decision_grade,
        "pivota_dimensions": {d.dimension: d.status for d in pivota.dimensions},
        "gaps": {d.dimension: d.gaps for d in pivota.dimensions if d.gaps},
    }


def _aggregate(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Pivota-vs-native roll-up over a set of scored per-SKU rows."""
    n = len(rows)
    return {
        "n": n,
        "pivota_decision_grade_rate": (
            round(sum(1 for r in rows if r["pivota_decision_grade"]) / n, 2) if n else 0.0
        ),
        "pivota_overall_avg": round(sum(r["pivota_overall"] for r in rows) / n, 2) if n else 0.0,
        "native_overall_avg": round(sum(r["native_overall"] for r in rows) / n, 2) if n else 0.0,
        "avg_overall_advantage": (
            round(sum(r["overall_advantage"] for r in rows) / n, 2) if n else 0.0
        ),
    }


async def _drive(args: argparse.Namespace, *, db: Any = database) -> Dict[str, Any]:
    was_connected = await _connect_if_needed(db)
    try:
        per_sku: List[Dict[str, Any]] = []
        for product_key in args.product_keys:
            try:
                record = await _assemble_record(product_key, db=db)
                comparison = compare_decision_grade(record)
                per_sku.append(_per_sku_row(product_key, comparison))
            except Exception as exc:  # noqa: BLE001 -- surface, don't abort the batch
                per_sku.append({"product_key": product_key, "error": str(exc)})
    finally:
        await _disconnect_if_needed(db, was_connected)

    scored = [r for r in per_sku if "error" not in r]
    aggregate = {**_aggregate(scored), "errors": len(per_sku) - len(scored)}
    # Per-category breakdown: the cohort spans skincare / haircare / supplement,
    # so a flat rate hides where each category module's data pays off.
    categories = sorted({r["category_kind"] for r in scored})
    by_category = {
        ck: _aggregate([r for r in scored if r["category_kind"] == ck]) for ck in categories
    }
    return {"aggregate": aggregate, "by_category": by_category, "per_sku": per_sku}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--product-key",
        dest="product_keys",
        action="append",
        default=[],
        help="A product_key to evaluate (repeatable).",
    )
    parser.add_argument(
        "--product-keys-file",
        help="Path to a file with one product_key per line.",
    )
    args = parser.parse_args()
    if args.product_keys_file:
        with open(args.product_keys_file, "r", encoding="utf-8") as handle:
            args.product_keys.extend(
                line.strip() for line in handle if line.strip() and not line.startswith("#")
            )
    if not args.product_keys:
        parser.error("provide at least one --product-key or --product-keys-file")
    return args


def main() -> int:
    args = _parse_args()
    report = asyncio.run(_drive(args))
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
