#!/usr/bin/env python3
"""
Check order details in database
"""
import psycopg2
from urllib.parse import urlparse

# Parse DATABASE_URL
DATABASE_URL = "postgresql://postgres:VJSIqWOeDbZOPZsLFJBxBKKOCFfNhLJv@junction.proxy.rlwy.net:15637/railway"

result = urlparse(DATABASE_URL)
username = result.username
password = result.password
database = result.path[1:]
hostname = result.hostname
port = result.port

# Connect and check recent orders
try:
    conn = psycopg2.connect(
        database=database,
        user=username,
        password=password,
        host=hostname,
        port=port
    )
    cur = conn.cursor()
    
    print("=== Checking Recent Orders ===\n")
    
    # Get last 5 orders with their payment info
    cur.execute("""
        SELECT order_id, merchant_id, total, currency, 
               payment_status, payment_intent_id, client_secret, 
               created_at
        FROM orders 
        WHERE merchant_id = 'merch_208139f7600dbf42'
        ORDER BY created_at DESC 
        LIMIT 5
    """)
    
    orders = cur.fetchall()
    
    for order in orders:
        print(f"Order ID: {order[0]}")
        print(f"  Merchant: {order[1]}")
        print(f"  Total: ${order[2]} {order[3]}")
        print(f"  Payment Status: {order[4]}")
        print(f"  Payment Intent ID: {order[5] or 'NULL'}")
        print(f"  Client Secret: {order[6][:30] if order[6] else 'NULL'}...")
        print(f"  Created: {order[7]}")
        print()
    
    cur.close()
    conn.close()
    
except Exception as e:
    print(f"Error: {e}")


