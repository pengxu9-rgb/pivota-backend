#!/usr/bin/env python3
"""Audit and optionally repair non-positive browse history prices."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db.database import database
from routes.accounts_orders_api import (
    _coerce_positive_history_price,
    _ensure_browse_history_schema,
    _history_item_key,
    _normalize_history_merchant_id,
    _resolve_history_price_lookup,
    shop_browse_history_events,
)


async def _fetch_rows(limit: int) -> List[Dict[str, Any]]:
    rows = await database.fetch_all(
        """
        SELECT id, user_id, product_id, merchant_id, price, currency, viewed_at
        FROM shop_browse_history_events
        WHERE price IS NULL OR price <= 0
        ORDER BY viewed_at DESC, id DESC
        LIMIT :limit
        """,
        {"limit": limit},
    )
    return [dict(row) for row in rows or []]


async def _run(args: argparse.Namespace) -> int:
    if not getattr(database, "is_connected", False):
        await database.connect()
    try:
        await _ensure_browse_history_schema()
        rows = await _fetch_rows(args.limit)
        lookup = await _resolve_history_price_lookup(rows)

        resolved = []
        unresolved = []
        for row in rows:
            key = _history_item_key(
                str(row.get("product_id") or "").strip(),
                _normalize_history_merchant_id(row.get("merchant_id")),
            )
            price = _coerce_positive_history_price(row.get("price"))
            resolution = lookup.get(key)
            if price is not None:
                continue
            if not resolution:
                unresolved.append(
                    {
                        "id": row.get("id"),
                        "user_id": row.get("user_id"),
                        "product_id": row.get("product_id"),
                        "merchant_id": row.get("merchant_id"),
                        "reason": "price_unresolved",
                    }
                )
                continue

            resolved.append(
                {
                    "id": row.get("id"),
                    "user_id": row.get("user_id"),
                    "product_id": row.get("product_id"),
                    "merchant_id": row.get("merchant_id"),
                    "price": resolution.price,
                    "currency": resolution.currency,
                    "source": resolution.source,
                }
            )

        if args.fix:
            for item in resolved:
                await database.execute(
                    shop_browse_history_events.update()
                    .where(shop_browse_history_events.c.id == item["id"])
                    .values(
                        price=float(item["price"]),
                        **({"currency": item["currency"]} if item.get("currency") else {}),
                    )
                )

        print(
            json.dumps(
                {
                    "mode": "fix" if args.fix else "dry_run",
                    "scanned": len(rows),
                    "resolved": len(resolved),
                    "unresolved": len(unresolved),
                    "resolved_sample": resolved[: args.sample],
                    "unresolved_sample": unresolved[: args.sample],
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    finally:
        if getattr(database, "is_connected", False):
            await database.disconnect()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=500, help="Maximum bad rows to scan.")
    parser.add_argument("--sample", type=int, default=20, help="Sample rows to print.")
    parser.add_argument("--fix", action="store_true", help="Update resolved rows.")
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
