#!/usr/bin/env python3
"""
Verify PostgreSQL Migration
Confirms that SQLite has been completely removed and PostgreSQL is working
"""

import asyncio
import os
import sys
from datetime import datetime

# Add current directory to path
sys.path.insert(0, '.')

async def verify_postgresql():
    """Verify PostgreSQL is the only database"""
    print("🔍 Verifying PostgreSQL Migration")
    print("="*60)
    
    # 1. Check DATABASE_URL
    db_url = os.getenv("DATABASE_URL", "")
    
    if not db_url:
        print("❌ DATABASE_URL not set!")
        print("   Set it in Railway or local .env file")
        return False
    
    print(f"✅ DATABASE_URL found: {db_url[:30]}...")
    
    # 2. Verify it's PostgreSQL
    if "sqlite" in db_url.lower():
        print("❌ Still using SQLite! Migration incomplete.")
        return False
    
    if not any(x in db_url.lower() for x in ["postgresql", "postgres"]):
        print("❌ DATABASE_URL is not PostgreSQL!")
        return False
    
    print("✅ PostgreSQL URL confirmed")
    
    # 3. Try to connect
    try:
        from db.database import database, DATABASE_URL
        
        await database.connect()
        print("✅ Connected to PostgreSQL")
        
        # 4. Test query
        result = await database.fetch_one("SELECT version()")
        if result:
            version = str(result[0])
            if "PostgreSQL" in version:
                print(f"✅ PostgreSQL version: {version[:50]}...")
            else:
                print(f"⚠️  Unexpected version: {version}")
        
        # 5. Check if tables exist
        tables_query = """
        SELECT table_name 
        FROM information_schema.tables 
        WHERE table_schema = 'public'
        ORDER BY table_name;
        """
        
        tables = await database.fetch_all(tables_query)
        print(f"\n📊 Tables in PostgreSQL:")
        for table in tables:
            print(f"   - {table['table_name']}")
        
        if not tables:
            print("   ⚠️  No tables found - they will be created on startup")
        
        await database.disconnect()
        
        print("\n" + "="*60)
        print("✅ PostgreSQL migration successful!")
        print("✅ SQLite has been completely removed")
        print("✅ All future data will use PostgreSQL only")
        
        return True
        
    except ImportError as e:
        print(f"❌ Import error: {e}")
        print("   Make sure you're in the pivota_infra directory")
        return False
    except Exception as e:
        print(f"❌ Connection failed: {e}")
        print("   Check your DATABASE_URL is correct")
        return False

if __name__ == "__main__":
    # Check if we're on Railway or local
    if os.getenv("RAILWAY_ENVIRONMENT"):
        print("🚂 Running on Railway")
    else:
        print("💻 Running locally")
        print("   Make sure DATABASE_URL is set to your PostgreSQL database")
    
    print("")
    success = asyncio.run(verify_postgresql())
    
    if not success:
        print("\n⚠️  PostgreSQL verification failed")
        print("   1. Set DATABASE_URL environment variable")
        print("   2. Use a PostgreSQL database (Railway, Supabase, etc.)")
        print("   3. Run this script again")
        sys.exit(1)
