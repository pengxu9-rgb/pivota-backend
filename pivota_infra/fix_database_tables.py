#!/usr/bin/env python3
"""
Fix Database Tables in PostgreSQL
Ensures all tables are created properly
"""

import asyncio
import sys
from sqlalchemy import create_engine, text
import os

# Add current directory to path
sys.path.insert(0, '.')

from db.database import metadata, database, DATABASE_URL
from db.orders import orders
from db.merchant_onboarding import merchant_onboarding
from db.products import products_cache, merchant_analytics
from db.agents import agents, agent_usage_logs
from utils.logger import logger

async def fix_tables():
    """Create all missing tables"""
    try:
        # Connect to database
        await database.connect()
        logger.info(f"Connected to database: {DATABASE_URL}")
        
        # Create all tables from metadata
        logger.info("Creating/updating all tables...")
        
        # Use raw SQL to ensure tables are created
        engine = create_engine(str(DATABASE_URL))
        
        # Create all tables defined in metadata
        metadata.create_all(engine)
        
        logger.info("✅ All tables created/verified")
        
        # Verify orders table exists
        result = await database.fetch_one("SELECT COUNT(*) as count FROM orders")
        logger.info(f"Orders table exists with {result['count']} records")
        
        # Verify other critical tables
        tables_to_check = [
            "merchant_onboarding",
            "orders", 
            "products_cache",
            "agents",
            "agent_usage_logs"
        ]
        
        for table_name in tables_to_check:
            try:
                result = await database.fetch_one(f"SELECT COUNT(*) as count FROM {table_name}")
                logger.info(f"✅ {table_name}: {result['count']} records")
            except Exception as e:
                logger.error(f"❌ {table_name}: {e}")
        
        await database.disconnect()
        logger.info("Database fixed successfully!")
        
    except Exception as e:
        logger.error(f"Error fixing database: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    print("🔧 Fixing Database Tables...")
    print("="*50)
    asyncio.run(fix_tables())
    print("="*50)
    print("✅ Database tables fixed!")
    print("")
    print("Now redeploy to Railway:")
    print("1. git add -A")
    print("2. git commit -m 'fix: ensure all database tables are created'")
    print("3. git push")
