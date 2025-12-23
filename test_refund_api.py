#!/usr/bin/env python3
"""
Refund DB debug helper (manual run only).

This file used to contain a hard-coded production DB URL and a `test_*` function.
It is now a safe CLI tool:
- No secrets in source (read from env)
- No DB connection at import-time
"""
from __future__ import annotations

import argparse
import asyncio
import os


async def main() -> int:
    parser = argparse.ArgumentParser(description="Query refunds for an order (debug helper).")
    parser.add_argument(
        "--order-id",
        default=os.getenv("REFUND_TEST_ORDER_ID"),
        help="Order id to query (or set REFUND_TEST_ORDER_ID).",
    )
    args = parser.parse_args()

    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        print("Missing DATABASE_URL.")
        return 2

    if not args.order_id:
        print("Missing --order-id (or set REFUND_TEST_ORDER_ID).")
        return 2

    import asyncpg

    print("Connecting to database...")
    conn = await asyncpg.connect(database_url)

    try:
        order_id = args.order_id

        print(f"\nTesting refund query for order: {order_id}")
        query = """
        SELECT
            refund_id,
            amount,
            currency,
            reason,
            source,
            status,
            created_by,
            created_at,
            processed_at,
            error_message,
            psp_refund_id,
            idempotency_key,
            raw_payload as metadata,
            CASE
                WHEN status = 'completed' THEN 'success'
                WHEN status = 'failed' THEN 'error'
                WHEN status = 'pending' THEN 'warning'
                ELSE 'info'
            END as status_type,
            CASE
                WHEN processed_at IS NOT NULL
                THEN EXTRACT(EPOCH FROM (processed_at - created_at))
                ELSE NULL
            END as processing_time_seconds
        FROM refund_records
        WHERE order_id = $1
        ORDER BY created_at DESC
        """

        refunds = await conn.fetch(query, order_id)

        print("Query executed successfully.")
        print(f"Found {len(refunds)} refund(s)")

        if refunds:
            for i, refund in enumerate(refunds, 1):
                print(f"\nRefund {i}:")
                print(f"  id: {refund['refund_id']}")
                print(f"  amount: {refund['amount']} {refund['currency']}")
                print(f"  status: {refund['status']}")
                print(f"  metadata: {refund['metadata']}")
        else:
            print("No refunds found for this order.")

        print("\nChecking order details...")
        order_query = """
        SELECT order_id, total, total_refunded, payment_status, currency
        FROM orders
        WHERE order_id = $1
        """
        order = await conn.fetchrow(order_query, order_id)

        if order:
            print("Order found:")
            print(f"  total: {order['total']}")
            print(f"  total_refunded: {order['total_refunded']}")
            print(f"  payment_status: {order['payment_status']}")
            print(f"  currency: {order['currency']}")
        else:
            print("Order not found.")
            return 1

    finally:
        await conn.close()
        print("\nDatabase connection closed.")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
