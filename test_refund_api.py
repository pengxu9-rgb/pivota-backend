#!/usr/bin/env python3
"""
Test refund API directly
"""
import asyncio
import asyncpg

DATABASE_URL = "postgresql://postgres:dkDuBgsfeuvwxkiRssSEQsBjsvcAuMjN@metro.proxy.rlwy.net:19541/railway"

async def test_refund_query():
    print("🔧 Connecting to database...")
    conn = await asyncpg.connect(DATABASE_URL)
    
    try:
        order_id = "ORD_EF2D82FAE417D1A2"
        
        # Test the exact query from refund_service
        print(f"\n📋 Testing refund query for order: {order_id}")
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
        
        print(f"✅ Query executed successfully!")
        print(f"📊 Found {len(refunds)} refund(s)")
        
        if refunds:
            for i, refund in enumerate(refunds, 1):
                print(f"\n  Refund {i}:")
                print(f"    - ID: {refund['refund_id']}")
                print(f"    - Amount: {refund['amount']} {refund['currency']}")
                print(f"    - Status: {refund['status']}")
                print(f"    - Metadata: {refund['metadata']}")
        else:
            print("  ℹ️  No refunds found for this order")
        
        # Check order details
        print(f"\n📦 Checking order details...")
        order_query = """
        SELECT order_id, total, total_refunded, payment_status, currency
        FROM orders 
        WHERE order_id = $1
        """
        order = await conn.fetchrow(order_query, order_id)
        
        if order:
            print(f"  ✅ Order found:")
            print(f"    - Total: {order['total']}")
            print(f"    - Total Refunded: {order['total_refunded']}")
            print(f"    - Payment Status: {order['payment_status']}")
            print(f"    - Currency: {order['currency']}")
        else:
            print(f"  ❌ Order not found!")
            
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        await conn.close()
        print("\n🔌 Database connection closed")

if __name__ == "__main__":
    asyncio.run(test_refund_query())

