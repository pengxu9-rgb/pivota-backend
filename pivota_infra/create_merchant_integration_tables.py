#!/usr/bin/env python3
"""
Create Merchant Integration Tables
Run this script to create the necessary tables for merchant integrations
"""
import asyncio
from db.database import database
from db.merchant_integrations import create_merchant_integrations_tables
from utils.logger import logger

async def main():
    """Create merchant integration tables"""
    try:
        logger.info("Connecting to database...")
        await database.connect()
        
        logger.info("Creating merchant integration tables...")
        success = await create_merchant_integrations_tables()
        
        if success:
            logger.info("✅ All merchant integration tables created successfully!")
        else:
            logger.error("❌ Failed to create some tables")
        
        await database.disconnect()
        logger.info("Database connection closed")
        
    except Exception as e:
        logger.error(f"Error creating tables: {e}")
        await database.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
