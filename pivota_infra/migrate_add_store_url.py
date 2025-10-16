"""
Migration script to add store_url column to merchant_onboarding table
Run this once on Render to update existing database schema
"""

import asyncio
from sqlalchemy import text
from pivota_infra.db.database import database, DATABASE_URL
from pivota_infra.utils.logger import logger

async def migrate_add_store_url(skip_connect=False):
    """Add store_url column to merchant_onboarding table if it doesn't exist
    
    Args:
        skip_connect: If True, assumes database is already connected (used during app startup)
    """
    try:
        if not skip_connect:
            await database.connect()
        
        # Check if store_url column exists
        check_query = text("""
            SELECT column_name 
            FROM information_schema.columns 
            WHERE table_name='merchant_onboarding' 
            AND column_name='store_url';
        """)
        result = await database.fetch_one(check_query)
        
        if result:
            logger.info("✅ store_url column already exists, no migration needed")
            return
        
        # Add store_url column (nullable first to avoid errors on existing rows)
        logger.info("📝 Adding store_url column to merchant_onboarding table...")
        add_column_query = text("""
            ALTER TABLE merchant_onboarding 
            ADD COLUMN IF NOT EXISTS store_url VARCHAR(500);
        """)
        await database.execute(add_column_query)
        
        # Update existing rows with website value or placeholder
        logger.info("📝 Populating store_url for existing merchants...")
        update_query = text("""
            UPDATE merchant_onboarding 
            SET store_url = COALESCE(website, 'https://placeholder.com')
            WHERE store_url IS NULL;
        """)
        await database.execute(update_query)
        
        # Now make it NOT NULL
        logger.info("📝 Setting store_url as NOT NULL...")
        alter_not_null_query = text("""
            ALTER TABLE merchant_onboarding 
            ALTER COLUMN store_url SET NOT NULL;
        """)
        await database.execute(alter_not_null_query)
        
        logger.info("✅ Migration completed successfully!")
        
    except Exception as e:
        logger.error(f"❌ Migration failed: {e}")
        raise
    finally:
        if not skip_connect:
            await database.disconnect()

if __name__ == "__main__":
    print("🔄 Running migration: add store_url column")
    print(f"📊 Database: {DATABASE_URL}")
    asyncio.run(migrate_add_store_url())
    print("✅ Migration complete!")

