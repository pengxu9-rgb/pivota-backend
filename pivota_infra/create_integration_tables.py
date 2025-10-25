"""Create integration tables in PostgreSQL if they don't exist"""
from db import database
import asyncio

async def create_tables():
    """Create merchant_stores and merchant_psps tables if they don't exist"""
    await database.connect()
    
    # Create merchant_stores table
    create_stores_table = """
    CREATE TABLE IF NOT EXISTS merchant_stores (
        store_id VARCHAR(50) PRIMARY KEY,
        merchant_id VARCHAR(50) NOT NULL,
        platform VARCHAR(50) NOT NULL,
        name VARCHAR(255) NOT NULL,
        domain VARCHAR(255),
        api_key TEXT,
        status VARCHAR(50) DEFAULT 'connected',
        connected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        last_sync TIMESTAMP,
        product_count INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """
    
    # Create merchant_psps table
    create_psps_table = """
    CREATE TABLE IF NOT EXISTS merchant_psps (
        psp_id VARCHAR(50) PRIMARY KEY,
        merchant_id VARCHAR(50) NOT NULL,
        provider VARCHAR(50) NOT NULL,
        name VARCHAR(255) NOT NULL,
        api_key TEXT,
        account_id VARCHAR(255),
        capabilities TEXT,
        status VARCHAR(50) DEFAULT 'active',
        connected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """
    
    try:
        await database.execute(create_stores_table)
        print("✓ merchant_stores table created/verified")
        
        await database.execute(create_psps_table)
        print("✓ merchant_psps table created/verified")
        
        # Create indexes for better performance
        await database.execute("CREATE INDEX IF NOT EXISTS idx_merchant_stores_merchant_id ON merchant_stores(merchant_id)")
        await database.execute("CREATE INDEX IF NOT EXISTS idx_merchant_psps_merchant_id ON merchant_psps(merchant_id)")
        print("✓ Indexes created/verified")
        
    except Exception as e:
        print(f"Error creating tables: {e}")
    finally:
        await database.disconnect()

if __name__ == "__main__":
    asyncio.run(create_tables())






