#!/usr/bin/env python3
"""
Temporary script to update Adyen API key in database
"""
import asyncio
import sys
from databases import Database

DATABASE_URL = "postgresql://postgres:VJSIqWOeDbZOPZsLFJBxBKKOCFfNhLJv@junction.proxy.rlwy.net:15637/railway"

async def update_adyen_key():
    print("=" * 60)
    print("Update Adyen API Key")
    print("=" * 60)
    print()
    
    api_key = input("Enter your working Adyen API key: ").strip()
    merchant_account = input("Enter Merchant Account (default: WoopayECOM): ").strip() or "WoopayECOM"
    
    if not api_key:
        print("❌ API key is required")
        return
    
    print()
    print(f"Updating Adyen config for merchant merch_208139f7600dbf42...")
    print(f"  API Key: {api_key[:15]}...")
    print(f"  Merchant Account: {merchant_account}")
    print()
    
    confirm = input("Proceed? (y/n): ").strip().lower()
    if confirm != 'y':
        print("Cancelled")
        return
    
    database = Database(DATABASE_URL)
    
    try:
        await database.connect()
        
        # Update the most recent Adyen config
        result = await database.execute(
            """
            UPDATE merchant_psps
            SET api_key = :api_key,
                account_id = :account_id,
                status = 'active'
            WHERE merchant_id = 'merch_208139f7600dbf42'
              AND provider = 'adyen'
              AND psp_id IN (
                SELECT psp_id FROM merchant_psps
                WHERE merchant_id = 'merch_208139f7600dbf42' AND provider = 'adyen'
                ORDER BY connected_at DESC
                LIMIT 1
              )
            """,
            {"api_key": api_key, "account_id": merchant_account}
        )
        
        print(f"\n✅ Updated {result} Adyen config(s)")
        
        # Verify
        verify = await database.fetch_one(
            """
            SELECT psp_id, LENGTH(api_key) as key_len, account_id, status
            FROM merchant_psps
            WHERE merchant_id = 'merch_208139f7600dbf42' AND provider = 'adyen'
            ORDER BY connected_at DESC
            LIMIT 1
            """
        )
        
        if verify:
            print(f"\n✅ Verified:")
            print(f"   PSP ID: {verify['psp_id']}")
            print(f"   API Key Length: {verify['key_len']}")
            print(f"   Account ID: {verify['account_id']}")
            print(f"   Status: {verify['status']}")
        
        await database.disconnect()
        
        print("\n🎉 Done! Now test Adyen payment:")
        print("   bash check_current_psps.sh")
        
    except Exception as e:
        print(f"\n❌ Error: {e}")
        await database.disconnect()

if __name__ == "__main__":
    asyncio.run(update_adyen_key())

