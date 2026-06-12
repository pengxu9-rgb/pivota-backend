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
from typing import Any, Dict, List

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


async def _assemble_record(product_key: str) -> Dict[str, Any]:
    payload = await _fetch_beauty_vertical_payload(product_key, None)
    offers = await resolve_pivot_offers(PivotOffersResolveRequest(product_key=product_key))
    best = offers.best_us_offer.model_dump() if offers.best_us_offer else None
    return eval_record_from_payload(payload, best_us_offer=best, alternatives=[])


def _per_sku_row(product_key: str, comparison: Dict[str, Any]) -> Dict[str, Any]:
    pivota = comparison["pivota"]
    native = comparison["native"]
    return {
        "product_key": product_key,
        "pivota_overall": pivota.overall,
        "native_overall": native.overall,
        "overall_advantage": comparison["overall_advantage"],
        "pivota_decision_grade": pivota.is_decision_grade,
        "pivota_dimensions": {d.dimension: d.status for d in pivota.dimensions},
        "gaps": {d.dimension: d.gaps for d in pivota.dimensions if d.gaps},
    }


async def _drive(args: argparse.Namespace, *, db: Any = database) -> Dict[str, Any]:
    was_connected = await _connect_if_needed(db)
    try:
        per_sku: List[Dict[str, Any]] = []
        for product_key in args.product_keys:
            try:
                record = await _assemble_record(product_key)
                comparison = compare_decision_grade(record)
                per_sku.append(_per_sku_row(product_key, comparison))
            except Exception as exc:  # noqa: BLE001 -- surface, don't abort the batch
                per_sku.append({"product_key": product_key, "error": str(exc)})
    finally:
        await _disconnect_if_needed(db, was_connected)

    scored = [r for r in per_sku if "error" not in r]
    n = len(scored)
    dims = ["find", "justify", "compare", "trust", "buy"]
    aggregate = {
        "n": n,
        "errors": len(per_sku) - n,
        "pivota_decision_grade_rate": (
            round(sum(1 for r in scored if r["pivota_decision_grade"]) / n, 2) if n else 0.0
        ),
        "pivota_overall_avg": round(sum(r["pivota_overall"] for r in scored) / n, 2) if n else 0.0,
        "native_overall_avg": round(sum(r["native_overall"] for r in scored) / n, 2) if n else 0.0,
        "avg_overall_advantage": (
            round(sum(r["overall_advantage"] for r in scored) / n, 2) if n else 0.0
        ),
    }
    return {"aggregate": aggregate, "per_sku": per_sku}


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
