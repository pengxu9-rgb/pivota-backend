#!/usr/bin/env python3
import asyncio
from databases import Database
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

async def cleanup_wix_stores():
    """Find and remove Wix store connections for cleanup"""
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        # Try Railway's PostgreSQL URL
        database_url = "postgresql://postgres:vZbOQGCYIemVIRMuYywKTRuJRPEUfJYU@roundhouse.proxy.rlwy.net:51005/railway"
    
    database = Database(database_url)
    
    try:
        await database.connect()
        print("🔍 Checking for existing Wix store connections...\n")
        
        # First, find all Wix stores
        query = """
            SELECT store_id, merchant_id, platform, domain, name, status, connected_at
            FROM merchant_stores
            WHERE platform = 'wix'
            ORDER BY connected_at DESC
        """
        
        stores = await database.fetch_all(query)
        
        if not stores:
            print("✅ No Wix stores found in database.")
            return
        
        print(f"Found {len(stores)} Wix store(s):\n")
        for idx, store in enumerate(stores, 1):
            print(f"{idx}. Store ID: {store['store_id']}")
            print(f"   Merchant ID: {store['merchant_id']}")
            print(f"   Domain/Site ID: {store['domain']}")
            print(f"   Name: {store['name']}")
            print(f"   Status: {store['status']}")
            print(f"   Connected: {store['connected_at']}")
            print()
        
        # Ask for confirmation
        if len(stores) > 0:
            print("\n⚠️  WARNING: This will delete ALL Wix store connections!")
            confirm = input("Do you want to delete all Wix stores? (yes/no): ")
            
            if confirm.lower() == 'yes':
                # Delete all Wix stores
                delete_query = "DELETE FROM merchant_stores WHERE platform = 'wix'"
                result = await database.execute(delete_query)
                print(f"\n✅ Deleted all Wix store connections.")
            else:
                print("\n❌ Cleanup cancelled.")
        
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        await database.disconnect()

if __name__ == "__main__":
    asyncio.run(cleanup_wix_stores())

