#!/usr/bin/env python3
"""
Fill missing psp_id in orders table
Match orders with merchant_psps based on merchant_id and psp_used
"""
import asyncio
from db.database import database
from utils.logger import logger

async def fix_psp_ids():
    """Fill psp_id for orders that have psp_used but missing psp_id"""
    try:
        await database.connect()
        logger.info("Connected to database")
        
        # First, check how many orders are missing psp_id
        check_query = """
            SELECT COUNT(*) as missing_count
            FROM orders
            WHERE psp_id IS NULL AND psp_used IS NOT NULL
        """
        result = await database.fetch_one(check_query)
        missing_count = result['missing_count'] if result else 0
        logger.info(f"Found {missing_count} orders with missing psp_id")
        
        if missing_count == 0:
            logger.info("✅ No orders need fixing")
            await database.disconnect()
            return
        
        # Update orders: join with merchant_psps to get psp_id based on merchant_id and provider
        update_query = """
            UPDATE orders o
            SET psp_id = mp.psp_id
            FROM merchant_psps mp
            WHERE o.merchant_id = mp.merchant_id
            AND LOWER(o.psp_used) = LOWER(mp.provider)
            AND o.psp_id IS NULL
            AND o.psp_used IS NOT NULL
            AND mp.status = 'active'
        """
        
        result = await database.execute(update_query)
        logger.info(f"✅ Updated {missing_count} orders with psp_id")
        
        # Verify the fix
        verify_query = """
            SELECT COUNT(*) as still_missing
            FROM orders
            WHERE psp_id IS NULL AND psp_used IS NOT NULL
        """
        verify_result = await database.fetch_one(verify_query)
        still_missing = verify_result['still_missing'] if verify_result else 0
        
        if still_missing > 0:
            logger.warning(f"⚠️ {still_missing} orders still missing psp_id (no matching PSP found)")
            
            # Show examples
            examples_query = """
                SELECT order_id, merchant_id, psp_used
                FROM orders
                WHERE psp_id IS NULL AND psp_used IS NOT NULL
                LIMIT 5
            """
            examples = await database.fetch_all(examples_query)
            logger.info("Examples of unfixed orders:")
            for ex in examples:
                logger.info(f"  - Order {ex['order_id']}: merchant={ex['merchant_id']}, psp_used={ex['psp_used']}")
        else:
            logger.info("✅ All orders now have psp_id!")
        
        await database.disconnect()
        logger.info("Done!")
        
    except Exception as e:
        logger.error(f"Error fixing psp_ids: {e}")
        import traceback
        traceback.print_exc()
        await database.disconnect()

if __name__ == "__main__":
    asyncio.run(fix_psp_ids())

