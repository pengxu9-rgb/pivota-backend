#!/usr/bin/env python3
"""
Check PSP configuration in database
"""
import psycopg2
import os
from urllib.parse import urlparse

# Parse DATABASE_URL from Railway
DATABASE_URL = "postgresql://postgres:VJSIqWOeDbZOPZsLFJBxBKKOCFfNhLJv@junction.proxy.rlwy.net:15637/railway"

# Parse connection details
result = urlparse(DATABASE_URL)
username = result.username
password = result.password
database = result.path[1:]
hostname = result.hostname
port = result.port

# Connect to database
try:
    conn = psycopg2.connect(
        database=database,
        user=username,
        password=password,
        host=hostname,
        port=port
    )
    cur = conn.cursor()
    
    print("Connected to database successfully!")
    
    # Check merchant_psps table
    print("\n=== Checking merchant_psps table ===")
    cur.execute("""
        SELECT merchant_id, provider, api_key, account_id, status, 
               LENGTH(api_key) as key_length, secret_key
        FROM merchant_psps 
        WHERE merchant_id = 'merch_208139f7600dbf42'
        ORDER BY connected_at DESC
    """)
    
    psps = cur.fetchall()
    if psps:
        for psp in psps:
            print(f"\nProvider: {psp[1]}")
            print(f"  Status: {psp[4]}")
            print(f"  API Key: {psp[2][:20]}... (length: {psp[5]})")
            print(f"  Account ID: {psp[3]}")
            print(f"  Secret Key: {'Yes' if psp[6] else 'No'}")
    else:
        print("No PSPs found for merchant merch_208139f7600dbf42")
    
    # Check if there are any PSPs at all
    print("\n=== All PSPs in database ===")
    cur.execute("SELECT COUNT(*), provider FROM merchant_psps GROUP BY provider")
    counts = cur.fetchall()
    for count in counts:
        print(f"{count[1]}: {count[0]} entries")
    
    cur.close()
    conn.close()
    
except Exception as e:
    print(f"Error: {e}")

