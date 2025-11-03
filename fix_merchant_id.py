import asyncio
import os
from databases import Database

async def fix_merchant_id():
    database_url = "postgresql://postgres:HJXUswMlQvvYvebIWYaJqrqOtMKZmaPg@junction.proxy.rlwy.net:47708/railway"
    database = Database(database_url)
    
    try:
        await database.connect()
        print("🔧 Fixing merchant@test.com merchant_id...")
        print("=" * 80)
        
        # Update users table
        query = """
        UPDATE users
        SET merchant_id = 'merch_208139f7600dbf42'
        WHERE email = 'merchant@test.com'
        RETURNING id, email, merchant_id
        """
        
        result = await database.fetch_one(query)
        
        if result:
            print(f"\n✅ Updated user account:")
            print(f"   User ID: {result['id']}")
            print(f"   Email: {result['email']}")
            print(f"   Merchant ID: {result['merchant_id']}")
        else:
            print("\n❌ No user found with email merchant@test.com")
        
        print("\n" + "=" * 80)
        print("✅ Fix complete! Try logging in again.")
        
    finally:
        await database.disconnect()

if __name__ == "__main__":
    asyncio.run(fix_merchant_id())
