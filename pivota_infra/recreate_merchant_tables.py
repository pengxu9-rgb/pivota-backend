"""
Script to recreate merchant tables with correct schema
Run this once to fix the database schema
"""
import asyncio
from db.database import database, metadata, engine
from db.merchants import merchants, kyb_documents

async def recreate_tables():
    """Drop and recreate merchant tables"""
    await database.connect()
    
    print("Dropping old merchant tables...")
    # Drop tables in reverse order (documents first due to foreign key)
    kyb_documents.drop(engine, checkfirst=True)
    merchants.drop(engine, checkfirst=True)
    
    print("Creating new merchant tables with correct schema...")
    # Create tables with new schema
    metadata.create_all(engine)
    
    print("✅ Merchant tables recreated successfully!")
    await database.disconnect()

if __name__ == "__main__":
    asyncio.run(recreate_tables())

