#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

os.environ.setdefault("DATABASE_URL", os.getenv("DATABASE_PUBLIC_URL", "postgresql://user:pass@localhost:5432/test"))

from db.database import database


QUERY = """
WITH reconciliations AS (
    SELECT
        oe.order_id,
        oe.created_at,
        oe.metadata,
        ROW_NUMBER() OVER (PARTITION BY oe.order_id ORDER BY oe.created_at DESC, oe.id DESC) AS rn
    FROM order_events oe
    WHERE oe.event_type = 'shopify_discount_reconciliation'
      AND oe.created_at >= :start_at
      AND oe.created_at < :end_at
),
created_orders AS (
    SELECT DISTINCT order_id
    FROM order_events
    WHERE event_type = 'shopify_order_created'
      AND created_at >= :start_at
      AND created_at < :end_at
),
suppressed AS (
    SELECT DISTINCT order_id
    FROM order_events
    WHERE event_type = 'shopify_receipt_suppressed'
)
SELECT
    o.order_id,
    o.merchant_id,
    o.customer_email,
    o.total,
    o.currency,
    o.payment_status,
    o.created_at,
    r.metadata AS reconciliation_metadata
FROM orders o
JOIN created_orders c ON c.order_id = o.order_id
JOIN reconciliations r ON r.order_id = o.order_id AND r.rn = 1
LEFT JOIN suppressed s ON s.order_id = o.order_id
WHERE s.order_id IS NULL
ORDER BY o.created_at ASC
"""


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    return value


async def _run(start_at: str, end_at: str) -> List[Dict[str, Any]]:
    await database.connect()
    try:
        rows = await database.fetch_all(QUERY, {"start_at": start_at, "end_at": end_at})
        return [dict(row) for row in (rows or [])]
    finally:
        await database.disconnect()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit paid Shopify orders created during the regression window that failed reconciliation without receipt suppression."
    )
    parser.add_argument("--start-at", required=True, help="Inclusive ISO timestamp, e.g. 2026-04-22T00:00:00Z")
    parser.add_argument("--end-at", required=True, help="Exclusive ISO timestamp, e.g. 2026-04-23T00:00:00Z")
    parser.add_argument("--format", choices={"json", "csv"}, default="json")
    parser.add_argument("--out", help="Optional output path")
    args = parser.parse_args()

    rows = asyncio.run(_run(args.start_at, args.end_at))
    normalized: List[Dict[str, Any]] = []
    for row in rows:
        reconciliation = row.get("reconciliation_metadata") or {}
        if isinstance(reconciliation, str):
            try:
                reconciliation = json.loads(reconciliation)
            except Exception:
                reconciliation = {"raw": reconciliation}
        normalized.append(
            {
                "order_id": row.get("order_id"),
                "merchant_id": row.get("merchant_id"),
                "customer_email": row.get("customer_email"),
                "payment_status": row.get("payment_status"),
                "currency": row.get("currency"),
                "total": str(row.get("total")) if row.get("total") is not None else None,
                "created_at": str(row.get("created_at")) if row.get("created_at") is not None else None,
                "shopify_order_id": (reconciliation or {}).get("shopify_order_id"),
                "reconciliation_status": (reconciliation or {}).get("status"),
                "mismatches": (reconciliation or {}).get("mismatches"),
                "expected": (reconciliation or {}).get("expected"),
                "observed": (reconciliation or {}).get("observed"),
            }
        )

    output_path = Path(args.out).expanduser() if args.out else None
    if args.format == "csv":
        fieldnames = [
            "order_id",
            "merchant_id",
            "customer_email",
            "payment_status",
            "currency",
            "total",
            "created_at",
            "shopify_order_id",
            "reconciliation_status",
            "mismatches",
            "expected",
            "observed",
        ]
        if output_path:
            handle = output_path.open("w", newline="", encoding="utf-8")
        else:
            handle = sys.stdout
        with handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for row in normalized:
                writer.writerow(
                    {
                        **row,
                        "mismatches": json.dumps(_jsonable(row.get("mismatches")), ensure_ascii=True),
                        "expected": json.dumps(_jsonable(row.get("expected")), ensure_ascii=True),
                        "observed": json.dumps(_jsonable(row.get("observed")), ensure_ascii=True),
                    }
                )
    else:
        payload = {
            "start_at": args.start_at,
            "end_at": args.end_at,
            "affected_order_count": len(normalized),
            "orders": _jsonable(normalized),
        }
        serialized = json.dumps(payload, indent=2, ensure_ascii=True)
        if output_path:
            output_path.write_text(serialized + "\n", encoding="utf-8")
        else:
            print(serialized)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
