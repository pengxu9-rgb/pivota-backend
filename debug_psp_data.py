"""
Debug script to diagnose PSP Overview showing 0 transactions
"""
import asyncio
import os
from datetime import datetime, timedelta
from db.database import database

async def debug_psp_data():
    await database.connect()
    
    print("\n" + "="*80)
    print("🔍 PSP Data Diagnosis")
    print("="*80)
    
    # 1. Check orders table
    print("\n1️⃣ Orders in database:")
    orders = await database.fetch_all("""
        SELECT order_id, merchant_id, psp_used, psp_id, payment_status, total, created_at
        FROM orders
        ORDER BY created_at DESC
        LIMIT 5
    """)
    for order in orders:
        print(f"  - Order: {order['order_id']}")
        print(f"    Merchant: {order['merchant_id']}")
        print(f"    PSP Used: '{order['psp_used']}' (type: {type(order['psp_used'])})")
        print(f"    PSP ID: '{order['psp_id']}' (type: {type(order['psp_id'])})")
        print(f"    Payment: {order['payment_status']}, ${order['total']}")
        print(f"    Created: {order['created_at']}")
        print()
    
    # 2. Check merchant_psps table
    print("\n2️⃣ PSP Configurations:")
    psps = await database.fetch_all("""
        SELECT psp_id, provider, merchant_id, status, connected_at
        FROM merchant_psps
        ORDER BY connected_at DESC
    """)
    for psp in psps:
        print(f"  - Provider: '{psp['provider']}' (type: {type(psp['provider'])})")
        print(f"    PSP ID: {psp['psp_id']}")
        print(f"    Merchant: {psp['merchant_id']}")
        print(f"    Status: {psp['status']}")
        print(f"    Connected: {psp['connected_at']}")
        print()
    
    # 3. Test the actual JOIN query
    print("\n3️⃣ Testing PSP Overview Query (last 7 days):")
    start_time = datetime.utcnow() - timedelta(days=7)
    
    # Original query from psp_overview_routes.py
    query = """
    WITH psp_stats AS (
        SELECT 
            mp.provider as psp_name,
            mp.psp_id as mp_psp_id,
            mp.status,
            COUNT(DISTINCT mp.merchant_id) as merchant_count,
            COUNT(o.order_id) as all_orders,
            COUNT(CASE WHEN LOWER(o.psp_used) = LOWER(mp.provider) THEN o.order_id END) as transaction_count,
            COUNT(CASE WHEN o.payment_status = 'paid' AND LOWER(o.psp_used) = LOWER(mp.provider) THEN 1 END) as success_count,
            COALESCE(SUM(CASE WHEN o.payment_status = 'paid' AND LOWER(o.psp_used) = LOWER(mp.provider) THEN o.total ELSE 0 END), 0) as total_volume
        FROM merchant_psps mp
        LEFT JOIN orders o ON o.merchant_id = mp.merchant_id 
            AND o.created_at >= :start_time
            AND ((o.psp_id IS NOT NULL AND mp.psp_id IS NOT NULL AND o.psp_id = mp.psp_id)
                 OR (o.psp_used IS NOT NULL AND LOWER(o.psp_used) = LOWER(mp.provider)))
        WHERE mp.status = 'active'
        GROUP BY mp.provider, mp.psp_id, mp.status
    )
    SELECT * FROM psp_stats
    """
    
    results = await database.fetch_all(query, {"start_time": start_time})
    
    for row in results:
        print(f"  - PSP: {row['psp_name']}")
        print(f"    Status: {row['status']}")
        print(f"    Merchant Count: {row['merchant_count']}")
        print(f"    All Orders Joined: {row['all_orders']}")
        print(f"    Transaction Count (after filter): {row['transaction_count']}")
        print(f"    Success Count: {row['success_count']}")
        print(f"    Total Volume: ${row['total_volume']}")
        print()
    
    # 4. Test simplified join - just merchant_id match
    print("\n4️⃣ Simplified JOIN test (merchant_id only, last 7 days):")
    simple = await database.fetch_all("""
        SELECT 
            mp.provider,
            mp.merchant_id,
            COUNT(o.order_id) as order_count,
            ARRAY_AGG(o.psp_used) as psp_used_values,
            ARRAY_AGG(o.psp_id) as psp_id_values
        FROM merchant_psps mp
        LEFT JOIN orders o ON o.merchant_id = mp.merchant_id
            AND o.created_at >= :start_time
        WHERE mp.status = 'active'
        GROUP BY mp.provider, mp.merchant_id
    """, {"start_time": start_time})
    
    for row in simple:
        print(f"  - Provider: {row['provider']}, Merchant: {row['merchant_id']}")
        print(f"    Order Count: {row['order_count']}")
        print(f"    PSP Used in Orders: {row['psp_used_values']}")
        print(f"    PSP IDs in Orders: {row['psp_id_values']}")
        print()
    
    # 5. Check if case mismatch
    print("\n5️⃣ Case sensitivity check:")
    case_check = await database.fetch_all("""
        SELECT DISTINCT
            o.psp_used as order_psp_used,
            mp.provider as config_provider,
            o.psp_used = mp.provider as exact_match,
            LOWER(o.psp_used) = LOWER(mp.provider) as case_insensitive_match
        FROM orders o
        CROSS JOIN merchant_psps mp
        WHERE o.psp_used IS NOT NULL
        LIMIT 10
    """)
    
    for row in case_check:
        print(f"  Order psp_used: '{row['order_psp_used']}' vs Config provider: '{row['config_provider']}'")
        print(f"    Exact match: {row['exact_match']}, Case-insensitive: {row['case_insensitive_match']}")
        print()
    
    await database.disconnect()
    print("\n" + "="*80)

if __name__ == "__main__":
    asyncio.run(debug_psp_data())

