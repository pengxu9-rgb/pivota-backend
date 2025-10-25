#!/usr/bin/env python3
"""Diagnose Checkout PSP configuration"""

import asyncio
import os
import sys
sys.path.append('./pivota_infra')
from db.database import database

async def main():
    # Connect to database
    if not database.is_connected:
        await database.connect()
    
    try:
        # 1. Check merchant_psps for Checkout config
        print("\n=== Checking merchant_psps table ===")
        psp_query = """
            SELECT merchant_id, provider, api_key, account_id, status, connected_at
            FROM merchant_psps
            WHERE merchant_id = :merchant_id AND provider = 'checkout'
            ORDER BY connected_at DESC
            LIMIT 1
        """
        psp_row = await database.fetch_one(
            psp_query,
            {"merchant_id": "merch_208139f7600dbf42"}
        )
        
        if psp_row:
            print(f"Found Checkout config:")
            print(f"  API Key: {psp_row['api_key'][:20]}... (len={len(psp_row['api_key']) if psp_row['api_key'] else 0})")
            print(f"  Account ID: {psp_row['account_id']}")
            print(f"  Status: {psp_row['status']}")
        else:
            print("No Checkout config found in merchant_psps")
            
        # 2. Test Checkout adapter directly
        print("\n=== Testing Checkout adapter ===")
        if psp_row and psp_row['api_key']:
            from adapters.psp_adapter import get_psp_adapter
            
            adapter_kwargs = {}
            if psp_row.get('account_id'):
                adapter_kwargs['public_key'] = psp_row['account_id']
                
            adapter = get_psp_adapter('checkout', psp_row['api_key'], **adapter_kwargs)
            print(f"Adapter created: {adapter}")
            print(f"  API Key in adapter: {adapter.api_key[:20]}...")
            print(f"  Base URL: {adapter.base_url}")
            
            # Try to create payment intent
            success, intent, error = await adapter.create_payment_intent(
                amount=9.99,
                currency="USD",
                metadata={"order_id": "TEST_ORDER", "customer_email": "test@example.com"}
            )
            
            if success and intent:
                print(f"\n✅ Payment intent created:")
                print(f"  ID: {intent.id}")
                print(f"  Client Secret: {intent.client_secret[:50]}..." if len(intent.client_secret) > 50 else f"  Client Secret: {intent.client_secret}")
                print(f"  Is URL: {intent.client_secret.startswith('http')}")
            else:
                print(f"\n❌ Failed: {error}")
                
        # 3. Check CHECKOUT_MODE env var
        print(f"\n=== Environment ===")
        print(f"CHECKOUT_MODE: {os.getenv('CHECKOUT_MODE', 'not set')}")
        
    finally:
        await database.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
